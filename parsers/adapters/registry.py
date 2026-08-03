from .bauhaus import BauhausAdapter
from .bauhof import BauhofAdapter
from .depo import DepoAdapter
from .ehituseabc import EhituseABCAdapter
from .espak import EspakAdapter
from .fere import FereAdapter
from .handymann import HandymannAdapter
from .public_commerce import PUBLIC_COMMERCE_ADAPTERS


ADAPTERS = {
    BauhausAdapter.code: BauhausAdapter,
    BauhofAdapter.code: BauhofAdapter,
    DepoAdapter.code: DepoAdapter,
    EhituseABCAdapter.code: EhituseABCAdapter,
    EspakAdapter.code: EspakAdapter,
    FereAdapter.code: FereAdapter,
    HandymannAdapter.code: HandymannAdapter,
}
ADAPTERS.update({adapter.code: adapter for adapter in PUBLIC_COMMERCE_ADAPTERS})


def get_adapter_class(code):
    return ADAPTERS.get(code)
