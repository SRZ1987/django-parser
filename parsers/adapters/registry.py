from .bauhof import BauhofAdapter
from .ehituseabc import EhituseABCAdapter
from .espak import EspakAdapter
from .fere import FereAdapter


ADAPTERS = {
    BauhofAdapter.code: BauhofAdapter,
    EhituseABCAdapter.code: EhituseABCAdapter,
    EspakAdapter.code: EspakAdapter,
    FereAdapter.code: FereAdapter,
}


def get_adapter_class(code):
    return ADAPTERS.get(code)
