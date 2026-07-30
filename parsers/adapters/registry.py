from .bauhaus import BauhausAdapter
from .bauhof import BauhofAdapter
from .depo import DepoAdapter
from .ehituseabc import EhituseABCAdapter
from .espak import EspakAdapter
from .fere import FereAdapter


ADAPTERS = {
    BauhausAdapter.code: BauhausAdapter,
    BauhofAdapter.code: BauhofAdapter,
    DepoAdapter.code: DepoAdapter,
    EhituseABCAdapter.code: EhituseABCAdapter,
    EspakAdapter.code: EspakAdapter,
    FereAdapter.code: FereAdapter,
}


def get_adapter_class(code):
    return ADAPTERS.get(code)
