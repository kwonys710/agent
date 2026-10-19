"""Scheduled Run 중복 실행 방지 (Phase 15, P1-1에서 OS 파일락으로 전환).

Windows Task Scheduler가 이전 실행이 끝나기 전에 다시 실행할 수 있으므로
파일 락으로 한 번에 하나만 돌게 한다. 분산 lock은 필요 없다.

누가 소유자인지는 **커널이 판정한다**(POSIX `flock`, Windows `msvcrt.locking` 바이트 0).
프로세스가 어떻게 죽든(SIGKILL·작업 종료 포함) 커널이 락을 풀므로 TTL을 기다릴 필요가 없다.
파일 내용(pid·created_at)은 사람이 읽는 진단 정보일 뿐 판정 근거가 아니다.

- 파일을 지워서 해제하지 않는다. 지우면 inode 동일성이 깨져 살아 있는 소유자가
  고아 inode의 락을 쥔 채 새 파일의 락이 성공한다(2중 실행). release()는 파일을 남긴다.
- `fcntl.lockf`(POSIX record lock)를 쓰지 않는다. 같은 프로세스가 같은 파일을 다른 fd로 열어
  닫기만 해도 락이 풀리고, 같은 프로세스의 두 번째 획득도 허용한다. `flock`은 fd 단위다.
- 락 API가 없거나 락이 동작하지 않는 환경(UNKNOWN)에서만 예전 TTL 판정으로 폴백한다.
"""
from __future__ import annotations

import errno
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from ..core.logger import get_logger

logger = get_logger("scheduler.lock")

try:  # POSIX
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - Windows
    _fcntl = None
try:  # Windows
    import msvcrt as _msvcrt
except ImportError:  # pragma: no cover - POSIX
    _msvcrt = None

# 락 판정 3상태
OWNED = "OWNED"          # 우리가 소유자
CONTENDED = "CONTENDED"  # 다른 프로세스가 커널 기준 살아 있는 채로 쥐고 있다
UNKNOWN = "UNKNOWN"      # 락이 동작하는지 알 수 없다 → TTL 폴백

MODE_KERNEL = "KERNEL"
MODE_TTL_FALLBACK = "TTL_FALLBACK"

# CONTENDED로 보는 errno. Windows LK_NBLCK 실패는 EACCES, 교착은 EDEADLK(EDEADLOCK)다.
_CONTENDED_ERRNOS = frozenset(
    getattr(errno, name)
    for name in ("EWOULDBLOCK", "EAGAIN", "EACCES", "EDEADLK")
    if hasattr(errno, name)
)

_O_BINARY = getattr(os, "O_BINARY", 0)  # Windows 텍스트 모드 변환 방지


class LockIOError(OSError):
    """lock 파일을 열거나 쓰지 못했다. False(=LOCKED, exit 0)가 아니라 예외로 올린다."""


class _Unreadable:
    """소유자 기록을 읽지 못했다(OSError). 파일이 없는 것(None)·깨진 것(dict)과 구분한다."""

    def __repr__(self) -> str:  # pragma: no cover - 디버그용
        return "UNREADABLE"


UNREADABLE = _Unreadable()


# --- locker (모듈을 인자로 받아 Linux에서 Windows 분기를 주입 테스트할 수 있다) ----------
def _classify(exc: OSError) -> tuple[str, str]:
    if exc.errno in _CONTENDED_ERRNOS:
        return CONTENDED, f"errno={exc.errno}"
    return UNKNOWN, f"락 오류 {exc!r}"


def _posix_lock(fd: int, mod: Any) -> tuple[str, str]:
    try:
        mod.flock(fd, mod.LOCK_EX | mod.LOCK_NB)
    except OSError as exc:
        return _classify(exc)
    return OWNED, ""


def _posix_unlock(fd: int, mod: Any) -> None:
    mod.flock(fd, mod.LOCK_UN)


def _win_lock(fd: int, mod: Any) -> tuple[str, str]:
    # msvcrt.locking은 '현재 파일 위치'부터 잠그므로 반드시 바이트 0으로 되돌린다.
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        mod.locking(fd, mod.LK_NBLCK, 1)
    except OSError as exc:
        return _classify(exc)
    return OWNED, ""


def _win_unlock(fd: int, mod: Any) -> None:
    os.lseek(fd, 0, os.SEEK_SET)
    mod.locking(fd, mod.LK_UNLCK, 1)


def _select_locker() -> Optional[tuple[Any, Any, Any]]:
    """fcntl → 없으면 msvcrt 순. 둘 다 없으면 None(TTL 폴백)."""
    if _fcntl is not None:
        return (_posix_lock, _posix_unlock, _fcntl)
    if _msvcrt is not None:
        return (_win_lock, _win_unlock, _msvcrt)
    return None


_LOCKER = _select_locker()


def _read_bytes(path: Path, offset: int) -> bytes:
    with open(path, "rb") as fh:
        fh.seek(offset)
        return fh.read()


@dataclass
class SchedulerLock:
    """실행 중 표시 파일. 소유자 판정은 OS 파일락이 한다."""

    path: Path
    stale_minutes: int = 60
    enabled: bool = True
    # 로그에 쓸 이름. 같은 lock 클래스를 Scheduled Run / Discovery / Canary가 함께 쓰므로,
    # 어느 실행이 잡고 있는지 문구로 분간되게 한다.
    label: str = "Scheduled Run"
    _acquired: bool = False
    _fd: Optional[int] = None
    _mode: str = ""
    # acquire 시점의 locker로 만든 unlock 수단 (함수, 모듈). 모듈 전역 _LOCKER를 release에서
    # 다시 읽으면 acquire 때와 어긋날 수 있다. 폴백 claim은 쥔 락이 없어 None이다.
    _unlock: Optional[tuple] = None
    _payload: dict = field(default_factory=dict)
    # acquire()가 False를 돌려준 뒤 호출자가 읽는다(읽기 전용). _log_blocked가 운영자에게 보여 주는
    # 값(소유자 pid·생성 시각·복구 안내 문장)을 그대로 담는다 — 호출자가 문구를 다시 쓰지 않게 한다.
    # lock은 DB를 쓰지 않는다: 이 값을 기록하는 일은 호출자 몫이다.
    blocked_info: dict = field(default_factory=dict)

    @property
    def mode(self) -> str:
        """획득에 성공했을 때의 소유 방식(KERNEL / TTL_FALLBACK). 못 잡았거나 비활성이면 빈 값."""
        return self._mode

    def acquire(self, now: Optional[datetime] = None) -> bool:
        """lock을 잡으면 True. 다른 실행이 진행 중이면 False. 파일 I/O 실패는 LockIOError."""
        if not self.enabled:
            return True

        if not self._acquired:
            # 인스턴스를 재사용해도 이전 시도의 값이 남지 않게 한다(mode/blocked_info 의 의미 유지).
            self._mode = ""
            self.blocked_info = {}

        now = now or datetime.now(timezone.utc)
        self.path.parent.mkdir(parents=True, exist_ok=True)

        try:
            # truncate 금지: 다른 소유자의 기록을 지우지 않는다.
            fd = os.open(self.path, os.O_CREAT | os.O_RDWR | _O_BINARY, 0o644)
        except OSError as exc:
            raise self._io_error("lock 파일을 열지 못했습니다", exc) from exc

        keep = False
        unlock: Optional[tuple] = None  # 이번 호출에서 락을 쥔 경우에만 채운다
        try:
            try:
                # 고아 chromium 등 자식 프로세스가 락을 물고 있지 않게 한다.
                os.set_inheritable(fd, False)
                created = os.fstat(fd).st_size == 0
                if created:
                    os.write(fd, b" ")  # Windows 바이트 0 락을 위한 자리바이트
            except OSError as exc:
                raise self._io_error("lock 파일 초기화에 실패했습니다", exc) from exc

            locker = _LOCKER  # acquire/release가 같은 locker를 쓰도록 한 번만 읽는다
            state, detail = self._try_lock(fd, locker)
            if state == OWNED:
                unlock = (locker[1], locker[2])
            # CONTENDED만 차단하고 UNKNOWN(locker 없음·EINVAL 등)은 TTL 폴백을 허용하는 것은 의도다.
            # 방금 만든 파일의 CONTENDED는 '경쟁을 관찰했는데 소유자가 없다'는 모순이라 락 영역 오판이거나
            # 초기화 중인 실행이므로 커널 락 없이 claim하지 않는다. UNKNOWN은 경쟁을 관찰한 적이 없고
            # 락 미지원 환경의 TTL 판정은 설계된 동작이라, 최초 생성 때 건너뛸 이유가 없다.
            if state == CONTENDED and created:
                # 방금 만든 파일을 정당하게 소유한 프로세스는 없다 → 락 영역 오판 신호이거나
                # 다른 실행이 초기화 중이다. 어느 쪽이든 커널 락 없이 claim하지 않고 막는다
                # (폴백 claim은 커널 락을 쥐지 않아 이후 실행이 2중 소유하게 된다).
                logger.warning(
                    "%s 방금 만든 lock 파일에서 락 충돌이 보고됨(%s) — 락 영역 오판 의심. "
                    "커널 락 없이는 소유하지 않으므로 이번 실행은 건너뜁니다(lock=%s).",
                    self.label, detail, self.path,
                )

            if state == CONTENDED:
                self._log_blocked(self._read(), now, mode=MODE_KERNEL)
                return False

            if state == OWNED:
                held = self._read()
                if (
                    isinstance(held, dict)
                    and held.get("mode") == MODE_TTL_FALLBACK
                    and not self._is_stale(held, now)
                ):
                    # 이전 소유자는 커널 락 없이 TTL 폴백으로 claim했다. 락이 비어 있어도
                    # 그가 살아 있는지 커널은 모른다 → TTL 규칙으로 보호한다(회귀 방지).
                    self._log_blocked(held, now, mode=MODE_TTL_FALLBACK)
                    return False
                self._log_previous_owner(held, now)
                self._write_payload(fd, now, mode=MODE_KERNEL)
                self._mode = MODE_KERNEL
                logger.info(
                    "%s lock 획득 mode=%s pid=%d lock=%s",
                    self.label, MODE_KERNEL, os.getpid(), self.path,
                )
            else:
                if not self._fallback_claim(fd, now, detail):
                    return False
                self._mode = MODE_TTL_FALLBACK

            self._fd = fd
            self._unlock = unlock
            self._acquired = True
            keep = True
            return True
        finally:
            if not keep:
                self._do_unlock(fd, unlock)
                os.close(fd)

    def release(self) -> None:
        """정상·비정상 종료 모두에서 호출된다. 멱등. 파일은 지우지 않는다."""
        if not self.enabled or not self._acquired or self._fd is None:
            return
        fd, self._fd = self._fd, None
        unlock, self._unlock = self._unlock, None
        self._acquired = False
        self._mode = ""
        try:
            # 반드시 락이 풀리기(close) 전에 쓴다. 풀린 뒤에 쓰면 새 소유자의 기록을 덮어쓴다.
            released = {
                **self._payload,
                "released_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
            self._write_payload(fd, payload=released)
        except OSError:  # pragma: no cover - 파일 잠김 등
            logger.warning("lock 해제 기록을 쓰지 못했습니다: %s", self.path)
        finally:
            # 명시적 unlock이 먼저다(Windows는 close만으로 풀리는 시점이 보장되지 않는다).
            # unlock 실패가 close를 막으면 fd를 쥔 채 끝난다.
            self._do_unlock(fd, unlock)
            os.close(fd)

    # --- 내부 ------------------------------------------------------------
    def _io_error(self, what: str, exc: OSError) -> LockIOError:
        logger.error("%s: %s (%s) — %s", what, self.path, exc, self.label)
        return LockIOError(f"{what}: {self.path}: {exc}")

    @staticmethod
    def _try_lock(fd: int, locker: Optional[tuple]) -> tuple[str, str]:
        if locker is None:
            return UNKNOWN, "락 API 없음(fcntl·msvcrt 모두 없음)"
        try:
            return locker[0](fd, locker[2])
        except Exception as exc:  # 예상 밖 예외도 판정 불가로 본다
            return UNKNOWN, f"예상 밖 예외 {exc!r}"

    def _do_unlock(self, fd: int, unlock: Optional[tuple]) -> None:
        if unlock is None:
            return  # 폴백 claim 등 쥔 락이 없다
        try:
            unlock[0](fd, unlock[1])
        except Exception:  # close가 어차피 푼다 — 여기서 막지 않는다
            logger.warning("lock unlock 호출이 실패했습니다(close로 해제): %s", self.path)

    def _write_payload(
        self,
        fd: int,
        now: Optional[datetime] = None,
        payload: Optional[dict] = None,
        mode: str = "",
    ) -> None:
        """offset 0부터 `" " + JSON` 을 쓰고 자른다. 선두 공백은 Windows 락 바이트다."""
        if payload is None:
            payload = {
                "pid": os.getpid(),
                "created_at": (now or datetime.now(timezone.utc)).isoformat(timespec="seconds"),
                "label": self.label,
                # 소유 방식을 남긴다. TTL_FALLBACK 소유자는 커널 락이 없어 다음 실행이 알아야 한다.
                "mode": mode,
            }
            self._payload = payload
        data = b" " + json.dumps(payload, ensure_ascii=False).encode("utf-8")
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
            os.ftruncate(fd, len(data))
        except OSError as exc:
            raise self._io_error("lock 기록을 쓰지 못했습니다", exc) from exc

    def _fallback_claim(self, fd: int, now: datetime, reason: str) -> bool:
        """락 판정이 불가능할 때만: 예전 TTL 판정. True면 기록까지 마쳤다."""
        logger.warning(
            "%s lock 기반 판정 비활성 mode=%s reason=%s — TTL(%d분) 판정으로 폴백합니다.",
            self.label, MODE_TTL_FALLBACK, reason, self.stale_minutes,
        )
        held = self._read()
        if held is UNREADABLE:
            self._log_blocked(held, now, mode=MODE_TTL_FALLBACK)
            return False
        if held is not None:
            if self._is_stale(held, now):
                if not held.get("released_at"):
                    logger.warning(
                        "오래된 %s lock을 회수합니다(생성 %s, TTL %d분).",
                        self.label, held.get("created_at"), self.stale_minutes,
                    )
            else:
                self._log_blocked(held, now, mode=MODE_TTL_FALLBACK)
                return False
        self._write_payload(fd, now, mode=MODE_TTL_FALLBACK)
        return True

    def _log_previous_owner(self, held: Any, now: datetime) -> None:
        """락을 얻었는데 기록에 해제 표시가 없다 = 이전 소유자가 정리 없이 사라졌다."""
        if not isinstance(held, dict) or held.get("released_at"):
            return
        if held.get("pid") is None and not held.get("created_at"):
            return  # 방금 만든 빈 파일이거나 깨진 기록: 이전 소유자의 흔적이 아니다
        age = self._age_seconds(held, now)
        logger.warning(
            "이전 %s 소유자 없음(pid=%s) — 기록이 %s초 전인데 커널 락이 비어 있다. "
            "비정상 종료로 보고 인수합니다. 이 로그가 매번 반복되면 파일시스템이 락을 지원하지 않는 것입니다.",
            self.label, held.get("pid"), "?" if age is None else int(age),
        )

    def _log_blocked(self, held: Any, now: datetime, *, mode: str) -> None:
        recovery = (
            "프로세스를 종료하면 즉시 해제됩니다. "
            "lock 파일을 지우지 마십시오(지우면 두 실행이 동시에 돌 수 있습니다)."
            if mode == MODE_KERNEL
            else f"커널 락 없이(TTL 판정) 소유된 lock이라 TTL({self.stale_minutes}분) 경과 후 자동 회수됩니다. "
            "lock 파일을 지우지 마십시오(지우면 두 실행이 동시에 돌 수 있습니다)."
        )
        self.blocked_info = {
            "label": self.label,
            "path": str(self.path),
            "mode": mode,
            "owner_pid": None,
            "owner_created_at": None,
            "stale_minutes": self.stale_minutes,
            "recovery": recovery,
        }
        if held is UNREADABLE:
            logger.warning(
                "다른 %s이 진행 중입니다(소유자 기록을 읽을 수 없습니다(락 보유 중), "
                "lock=%s, TTL %d분, mode=%s). %s",
                self.label, self.path, self.stale_minutes, mode, recovery,
            )
            return
        held = held or {}
        self.blocked_info["owner_pid"] = held.get("pid")
        self.blocked_info["owner_created_at"] = held.get("created_at")
        if mode == MODE_KERNEL and held.get("pid") is None and not held.get("created_at"):
            # 락은 충돌인데 소유자 기록이 비어 있다. 정상 소유자라면 자리바이트와 payload 사이
            # 수 마이크로초에만 보이는 상태다. 반복되면 FS의 락 보고 오류다. 종료할 pid가 없으므로
            # '프로세스를 종료하라'고 하지 않고, 운영자가 실제로 할 수 있는 행동을 적는다.
            message = (
                "다른 %s이 락을 쥐고 있다고 보고되지만 소유자 기록이 비어 있습니다(lock=%s, mode=%s). "
                "초기화 중인 정상 실행이면 곧 사라지는 일시 상태입니다. 이 로그가 실행마다 반복되면 "
                "파일시스템의 락 보고 오류일 가능성이 높습니다 — 실행 중인 %s 프로세스가 없는지 "
                "확인하고, 없는데 반복되면 lock 경로를 락을 지원하는 로컬 디스크로 옮기도록 운영자에게 "
                "알리십시오. lock 파일을 지우지 마십시오(지우면 두 실행이 동시에 돌 수 있습니다)."
            )
            args = (self.label, self.path, mode, self.label)
            self.blocked_info["recovery"] = message % args  # 로그에 찍히는 안내와 같은 문장
            logger.warning(message, *args)
            return
        age = self._age_seconds(held, now)
        too_long = age is not None and age >= max(self.stale_minutes, 1) * 60
        if too_long and mode == MODE_KERNEL:
            logger.warning(
                "다른 %s이 비정상적으로 긴 실행 중입니다 — 회수하지 않습니다. "
                "끝내려면 pid=%s 를 종료하십시오(생성 %s, TTL %d분, lock=%s, mode=%s). %s",
                self.label, held.get("pid"), held.get("created_at"),
                self.stale_minutes, self.path, mode, recovery,
            )
            return
        logger.info(
            "다른 %s이 진행 중입니다(pid=%s, 생성 %s, TTL %d분, lock=%s, mode=%s). %s",
            self.label, held.get("pid"), held.get("created_at"),
            self.stale_minutes, self.path, mode, recovery,
        )

    @staticmethod
    def _age_seconds(held: dict, now: datetime) -> Optional[float]:
        created = SchedulerLock._parse_time(held.get("created_at"))
        if created is None:
            return None
        return max(0.0, (now - created).total_seconds())

    @staticmethod
    def _parse_time(raw: Any) -> Optional[datetime]:
        if not raw:
            return None
        try:
            created = datetime.fromisoformat(str(raw))
        except ValueError:
            return None
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        return created

    def _read(self) -> Any:
        """None(파일 없음) / dict(기록, 깨졌으면 빈 값 dict) / UNREADABLE(OSError).

        Windows 바이트영역 락은 mandatory라 락된 바이트 0을 포함해 읽으면 실패한다.
        payload가 `" " + JSON` 이므로 offset 1부터 읽고, 안 되면 전체를 읽는다
        (선두 공백은 JSON 파서가 무시하므로 전체를 읽어도 유효하다).
        """
        try:
            try:
                raw = _read_bytes(self.path, 1)
            except FileNotFoundError:
                return None
            except OSError:
                raw = _read_bytes(self.path, 0)
        except FileNotFoundError:
            return None
        except OSError:
            return UNREADABLE  # sharing violation·미수화 placeholder 등: 깨진 파일이 아니다
        try:
            data = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            # 자리바이트 없는 예전 형식(`{...}`)은 offset 1에서 첫 글자가 잘린다. 전체를 다시 본다.
            # 예전 형식 파일은 offset 0 읽기가 실패하지 않으므로, 여기서의 OSError는 바이트 0에
            # mandatory lock이 걸려 있다(= 소유자가 살아 있다)는 뜻이다. '깨진 기록'(stale)이 아니라
            # UNREADABLE로 돌려 폴백이 회수하지 않게 한다.
            try:
                data = json.loads(_read_bytes(self.path, 0).decode("utf-8"))
            except OSError:
                return UNREADABLE
            except (json.JSONDecodeError, UnicodeDecodeError):
                return {"pid": None, "created_at": None}  # 깨진 lock: 폴백에서는 stale로 취급
        return data if isinstance(data, dict) else {}

    def _is_stale(self, held: dict, now: datetime) -> bool:
        """TTL 폴백 경로에서만 호출된다. 락 경로에서는 회수 조건이 아니다."""
        if held.get("released_at"):
            return True  # 소유자가 정상 해제했다
        created = self._parse_time(held.get("created_at"))
        if created is None:
            return True
        return now - created >= timedelta(minutes=max(self.stale_minutes, 1))

    def __enter__(self) -> "SchedulerLock":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.release()
