from .espak import EspakAdapter


ADAPTERS = {
    EspakAdapter.code: EspakAdapter,
}


def get_adapter_class(code):
    return ADAPTERS.get(code)
