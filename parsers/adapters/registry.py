from .bauhaus import BauhausAdapter
from .bauhof import BauhofAdapter
from .catalog_api_retailers import API_RETAILER_ADAPTERS
from .catalog_listing_retailers import LISTING_RETAILER_ADAPTERS
from .catalog_sitemap_retailers import CATALOG_SITEMAP_RETAILER_ADAPTERS
from .depo import DepoAdapter
from .ehituseabc import EhituseABCAdapter
from .espak import EspakAdapter
from .fere import FereAdapter
from .handymann import HandymannAdapter
from .lemona import LemonaAdapter
from .motonet import MotonetAdapter
from .oomipood import OomipoodAdapter
from .public_commerce import PUBLIC_COMMERCE_ADAPTERS
from .sitemap_retailers import EffexAdapter, VipexAdapter


ADAPTERS = {
    BauhausAdapter.code: BauhausAdapter,
    BauhofAdapter.code: BauhofAdapter,
    DepoAdapter.code: DepoAdapter,
    EhituseABCAdapter.code: EhituseABCAdapter,
    EspakAdapter.code: EspakAdapter,
    FereAdapter.code: FereAdapter,
    HandymannAdapter.code: HandymannAdapter,
    LemonaAdapter.code: LemonaAdapter,
    MotonetAdapter.code: MotonetAdapter,
    OomipoodAdapter.code: OomipoodAdapter,
    VipexAdapter.code: VipexAdapter,
    EffexAdapter.code: EffexAdapter,
}
ADAPTERS.update({adapter.code: adapter for adapter in PUBLIC_COMMERCE_ADAPTERS})
ADAPTERS.update({adapter.code: adapter for adapter in API_RETAILER_ADAPTERS})
ADAPTERS.update({adapter.code: adapter for adapter in LISTING_RETAILER_ADAPTERS})
ADAPTERS.update({adapter.code: adapter for adapter in CATALOG_SITEMAP_RETAILER_ADAPTERS})


def get_adapter_class(code):
    return ADAPTERS.get(code)
