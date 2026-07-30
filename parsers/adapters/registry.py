from .espak import EspakAdapter
from .fere import FereAdapter


ADAPTERS = {
    EspakAdapter.code: EspakAdapter,
    FereAdapter.code: FereAdapter,
}


def get_adapter_class(code):
    return ADAPTERS.get(code)
