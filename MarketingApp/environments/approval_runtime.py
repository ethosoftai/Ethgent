"""
Approval Runtime — `araclar/` icindeki duz fonksiyonlarin (worker_video_yayinla
gibi) insan onayi istemesini saglayan surec-genelinde kayit noktasi.

Neden bu modul var: onay isteme mantigi (`BaseModel.request_approval`) bir
sinif metodu, ama `araclar/` icindeki tool'lar duz modul-seviyesi fonksiyonlar
ve hicbir BaseModel referansi tutmuyorlar (`automation_runtime.py`/
`vlm_araclari.get_registered_bot()` ile ayni "surec-seviyesi kayit" deseni).

`BaseModel.__init__` her zaman burada DINAMIK bir kapanis (closure) kayit
eder — `self.request_approval`'i HER cagride yeniden okur, boylece
TerminalManager'in `base_model.request_approval`'i insana-soran bir
versiyonla degistirmesi (bkz. terminal.py) otomatik olarak yansir.

Bassiz (headless) baglamlar (worker.py, ve varsayilan olarak agent_api.py)
kendi HIZLI-REDDET handler'larini kaydeder: onaylayacak insan yoksa
`BaseModel`'in varsayilan 300sn'lik event-wait'ini beklemek yerine aninda ve
acikca reddeder (fail-closed, ama gereksiz beklemeden).
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Awaitable, Callable, Iterable, Iterator

ApprovalHandler = Callable[[str, str], Awaitable[bool]]

_handler: ApprovalHandler | None = None


def register_approval_handler(handler: ApprovalHandler | None) -> None:
    """Surec icin aktif onay isleyicisini kaydeder (son kayit kazanir)."""
    global _handler
    _handler = handler


def get_registered_approval_handler() -> ApprovalHandler | None:
    return _handler


async def request_tool_approval(action_id: str, description: str) -> bool:
    """`araclar/` icindeki bir tool'un onay istemesi icin TEK giris noktasi.

    Kayitli bir handler yoksa (hic kurulum yapilmamis surec) fail-closed
    davranir: onay YOK sayilir, sessizce izin verilmez.
    """
    if _handler is None:
        return False
    return await _handler(action_id, description)


async def reject_all_approvals(action_id: str, description: str) -> bool:
    """Bassiz baglamlar icin varsayilan handler: her istegi aninda reddeder."""
    print(
        f"🚫 [Approval] Reddedildi (bu surecte onaylayacak insan yok): "
        f"{action_id} — {description}"
    )
    return False


# ---------------------------------------------------------------- onay: isi baslatan verir
#
# Bir isi kuyruga koyan taraf (insan ya da yazma yetkisi olan bir uygulama) `payload.approved_tools`
# ile hangi tool'larin calisabilecegini ONCEDEN soyleyebilir. Bu liste modelin erisemedigi bir
# yerden gelir: model onu degistiremez, bir gorev metni de degistiremez. Bir tool'un onayi, o is
# calisirken listede adi geciyorsa verilir; baska hicbir sey onay sayilmaz.

_job_approvals: ContextVar[frozenset[str]] = ContextVar("job_approvals", default=frozenset())

MAX_JOB_APPROVALS = 32


def clean_approved_tools(value: object) -> frozenset[str]:
    """`payload.approved_tools`'u temizler: sadece makul uzunlukta metinlerden olusan, kisa bir liste."""
    if not isinstance(value, (list, tuple, set, frozenset)):
        return frozenset()
    names = [item.strip() for item in value if isinstance(item, str) and 0 < len(item.strip()) <= 128]
    return frozenset(names[:MAX_JOB_APPROVALS])


@contextmanager
def job_approvals(approved_tools: Iterable[str] | object) -> Iterator[frozenset[str]]:
    """Bu `with` blogu (ve icinden baslayan gorevler) boyunca verilen tool onaylarini gecerli kilar."""
    granted = clean_approved_tools(approved_tools)
    token = _job_approvals.set(granted)
    try:
        yield granted
    finally:
        _job_approvals.reset(token)


async def approve_if_granted_by_job(action_id: str, description: str) -> bool:
    """Bassiz worker icin handler: sadece isi baslatanin onceden onayladigi tool'lara izin verir."""
    if action_id in _job_approvals.get():
        print(f"✅ [Approval] Is baslatan tarafindan onaylanmisti: {action_id}")
        return True
    return await reject_all_approvals(action_id, description)
