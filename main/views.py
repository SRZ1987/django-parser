import logging
from urllib.parse import urlencode

from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth import get_user_model, login
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Case, Count, DecimalField, F, IntegerField, Q, Sum, Value, When
from django.db.models.functions import Coalesce
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.utils.encoding import force_str
from django.utils.http import urlsafe_base64_decode
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.translation import gettext as _
from django.views.decorators.http import require_GET, require_POST

from catalog.models import Category, ProductOffer, Shop
from catalog.services.normalization import normalize_product_name, tokenize
from catalog.services.product_search import (
    DEFAULT_PAGE_SIZE,
    build_token_candidate_query,
    paginate_group,
    search_products,
)

from .analytics import build_analytics_dashboard, record_store_click
from .email_verification import email_verification_token, send_verification_email
from .forms import EmailRequiredUserCreationForm, ResendConfirmationForm
from .group_purchases import (
    active_group_purchases,
    cleanup_group_purchases,
    close_group_if_empty,
    detach_group_purchase_membership,
    sync_shopping_list_group_memberships,
    touch_group_purchase,
)
from .models import (
    GroupPurchase,
    GroupPurchaseMember,
    GroupPurchaseMessage,
    ShoppingList,
    ShoppingListEvent,
    ShoppingListItem,
)
from .price_alerts import set_shopping_list_price_alerts
from .services import (
    add_offer_to_shopping_list,
    build_purchase_plan,
    get_best_offer,
    get_or_create_shopping_list,
    record_shopping_list_event,
    replace_shopping_list_offer,
)


CATALOG_PAGE_SIZE = 24
logger = logging.getLogger(__name__)
CATALOG_SORT_OPTIONS = {
    "relevance",
    "name_asc",
    "name_desc",
    "price_asc",
    "price_desc",
    "newest",
}


def product_offer_search_query(query):
    normalized_query = normalize_product_name(query)
    tokens = [token for token in tokenize(normalized_query) if len(token) >= 2][:6]
    phrase_query = (
        Q(original_name__icontains=query)
        | Q(original_name__icontains=normalized_query)
        | Q(normalized_name__icontains=normalized_query)
        | Q(search_text__icontains=normalized_query)
        | Q(sku__icontains=query)
        | Q(barcode__icontains=query)
        | Q(external_id__icontains=query)
        | Q(product__name__icontains=query)
        | Q(product__brand__icontains=query)
        | Q(product__model__icontains=query)
    )
    if not tokens:
        return phrase_query

    token_query = Q()
    for token in tokens:
        token_query &= (
            build_token_candidate_query(token)
            | Q(original_name__icontains=token)
            | Q(sku__icontains=token)
            | Q(barcode__icontains=token)
            | Q(external_id__icontains=token)
            | Q(product__name__icontains=token)
            | Q(product__brand__icontains=token)
            | Q(product__model__icontains=token)
        )
    return phrase_query | token_query


def available_offers():
    return ProductOffer.objects.filter(is_active=True, is_available=True)


def home(request):
    query = request.GET.get("q", "").strip()

    return render(
        request,
        "main/home.html",
        {
            "query": query,
        },
    )


def register(request):
    if request.user.is_authenticated:
        return redirect("shopping_list")

    if request.method == "POST":
        form = EmailRequiredUserCreationForm(request.POST)
        if form.is_valid():
            user = form.save()
            login(request, user)
            next_url = request.POST.get("next", "")
            if next_url and url_has_allowed_host_and_scheme(
                next_url,
                allowed_hosts={request.get_host()},
            ):
                return redirect(next_url)
            return redirect("shopping_list")
    else:
        form = EmailRequiredUserCreationForm()

    return render(
        request,
        "registration/register.html",
        {
            "form": form,
            "next": request.GET.get("next", ""),
        },
    )


def confirm_email(request, uidb64, token):
    user_model = get_user_model()
    try:
        user_id = force_str(urlsafe_base64_decode(uidb64))
        user = user_model.objects.get(pk=user_id)
    except (TypeError, ValueError, OverflowError, user_model.DoesNotExist):
        user = None

    confirmed = bool(user and email_verification_token.check_token(user, token))
    if confirmed:
        user.is_active = True
        user.save(update_fields=["is_active"])

    return render(
        request,
        "registration/email_confirmed.html",
        {"confirmed": confirmed},
        status=200 if confirmed else 400,
    )


def resend_confirmation(request):
    form = ResendConfirmationForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = (
            get_user_model()
            .objects.filter(email__iexact=form.cleaned_data["email"], is_active=False)
            .first()
        )
        if user:
            try:
                send_verification_email(request, user)
            except Exception:
                logger.exception("Could not resend verification email to user %s", user.pk)
                form.add_error(None, _("Could not send the email. Please try again later."))
                return render(request, "registration/resend_confirmation.html", {"form": form})
        return render(
            request,
            "registration/email_confirmation_sent.html",
            {"email": form.cleaned_data["email"]},
        )
    return render(request, "registration/resend_confirmation.html", {"form": form})


def product_search_view(request):
    query = request.GET.get("q", "").strip()
    results = search_products(query) if query else None
    results_page = paginate_group(
        results.matches if results else [],
        request.GET.get("page"),
        page_size=DEFAULT_PAGE_SIZE,
    )

    page_params = request.GET.copy()
    page_params.pop("page", None)
    list_offer_ids = get_list_offer_ids(request.user)

    return render(
        request,
        "main/search.html",
        {
            "query": query,
            "results": results,
            "results_page": results_page,
            "page_params": page_params.urlencode(),
            "list_offer_ids": list_offer_ids,
        },
    )


def active_categories(shop=None):
    categories = Category.objects.filter(
        offers__is_active=True,
        offers__is_available=True,
    )
    if shop:
        categories = categories.filter(shop=shop)
    return categories.distinct().order_by("name")


def apply_catalog_sorting(offers, sort, query):
    if sort == "name_desc":
        return offers.order_by("-original_name", "id")

    if sort == "price_asc":
        return _with_effective_price(offers).order_by("price_missing", "effective_price", "id")

    if sort == "price_desc":
        return _with_effective_price(offers).order_by("price_missing", "-effective_price", "id")

    if sort == "newest":
        return offers.order_by("-created_at", "-id")

    if sort == "relevance" and query:
        return (
            _with_effective_price(offers)
            .annotate(
                identifier_match_priority=Case(
                    When(
                        Q(barcode__iexact=query) | Q(product__barcode__iexact=query),
                        then=Value(0),
                    ),
                    When(Q(sku__iexact=query), then=Value(1)),
                    When(Q(external_id__iexact=query), then=Value(2)),
                    default=Value(3),
                    output_field=IntegerField(),
                )
            )
            .order_by(
                "identifier_match_priority",
                "price_missing",
                "effective_price",
                "original_name",
                "shop__name",
                "id",
            )
        )

    return offers.order_by("original_name", "id")


def _with_effective_price(offers):
    return offers.annotate(
        effective_price=Case(
            When(price__isnull=True, sale_price__isnull=False, then=F("sale_price")),
            When(sale_price__lt=F("price"), then=F("sale_price")),
            default=F("price"),
            output_field=DecimalField(max_digits=12, decimal_places=2),
        ),
        price_missing=Case(
            When(sale_price__isnull=True, price__isnull=True, then=Value(1)),
            default=Value(0),
            output_field=IntegerField(),
        ),
    )


def catalog_view(request):
    query = request.GET.get("q", "").strip()
    shop_code = request.GET.get("shop", "").strip()
    category_id = request.GET.get("category", "").strip()
    sort = request.GET.get("sort", "relevance").strip()
    if sort not in CATALOG_SORT_OPTIONS:
        sort = "relevance"

    shops = Shop.objects.filter(is_active=True).order_by("name")
    selected_shop = shops.filter(code=shop_code).first() if shop_code else None

    offers = available_offers().select_related("shop", "category", "product")

    if query:
        offers = offers.filter(product_offer_search_query(query))

    if selected_shop:
        offers = offers.filter(shop=selected_shop)

    categories = active_categories(selected_shop)
    selected_category = None
    if category_id.isdigit():
        selected_category = categories.filter(pk=int(category_id)).first()
        if selected_category:
            offers = offers.filter(category=selected_category)

    offers = apply_catalog_sorting(offers, sort, query)

    paginator = Paginator(offers, CATALOG_PAGE_SIZE)
    page_obj = paginator.get_page(request.GET.get("page"))

    page_params = request.GET.copy()
    page_params.pop("page", None)

    return render(
        request,
        "main/catalog.html",
        {
            "query": query,
            "shops": shops,
            "categories": categories,
            "selected_shop_code": selected_shop.code if selected_shop else "",
            "selected_category_id": str(selected_category.pk) if selected_category else "",
            "selected_sort": sort,
            "sort_options": [
                ("relevance", _("By relevance")),
                ("name_asc", _("Name: A–Z")),
                ("name_desc", _("Name: Z–A")),
                ("price_asc", _("Lowest price first")),
                ("price_desc", _("Highest price first")),
                ("newest", _("Newest first")),
            ],
            "page_obj": page_obj,
            "page_params": page_params.urlencode(),
            "page_range": paginator.get_elided_page_range(page_obj.number),
            "list_offer_ids": get_list_offer_ids(request.user),
        },
    )


def search_suggestions(request):
    query = request.GET.get("q", "").strip()

    if len(query) < 2:
        return JsonResponse({"results": []})

    offers = (
        available_offers()
        .filter(product_offer_search_query(query))
        .select_related("shop", "category", "product")
        .order_by("original_name")[:8]
    )

    return JsonResponse(
        {
            "results": [
                {
                    "id": offer.id,
                    "name": offer.original_name,
                    "shop": offer.shop.name,
                    "category": offer.category.name if offer.category else "",
                    "sku": offer.sku,
                    "barcode": offer.barcode,
                    "price": str(offer.price) if offer.price is not None else None,
                    "sale_price": str(offer.sale_price) if offer.sale_price is not None else None,
                    "quantity_price": str(offer.quantity_price) if offer.quantity_price is not None else None,
                    "quantity_price_min_quantity": offer.quantity_price_min_quantity,
                    "currency": offer.currency,
                    "image_url": offer.image_url,
                    "product_url": offer.product_url,
                    "detail_url": reverse("offer_detail", args=[offer.pk]),
                }
                for offer in offers
            ]
        }
    )


def offer_detail(request, pk):
    offer = get_object_or_404(
        available_offers().select_related("shop", "category", "product"),
        pk=pk,
    )
    return render(
        request,
        "main/offer_detail.html",
        {
            "offer": offer,
            "list_offer_ids": get_list_offer_ids(request.user),
        },
    )


def store_click(request, offer_pk):
    offer = get_object_or_404(
        ProductOffer.objects.select_related("shop"),
        pk=offer_pk,
    )
    if not offer.product_url:
        return redirect("offer_detail", pk=offer.pk)
    record_store_click(request, offer)
    return redirect(offer.product_url)


@staff_member_required(login_url="admin:login")
def statistics_dashboard(request):
    return render(
        request,
        "main/statistics_dashboard.html",
        build_analytics_dashboard(),
    )


@staff_member_required(login_url="admin:login")
@require_GET
def statistics_data(request):
    context = build_analytics_dashboard()
    return JsonResponse(
        {
            "html": render_to_string(
                "main/includes/statistics_live.html",
                context,
                request=request,
            ),
            "updated_at": timezone.localtime().isoformat(),
        }
    )


@login_required
def shopping_list(request):
    cleanup_group_purchases()
    user_list = get_or_create_shopping_list(request.user)
    sync_shopping_list_group_memberships(user_list)
    return render(
        request,
        "main/shopping_list.html",
        build_shopping_list_context(request, user_list, editable=True),
    )


@login_required
@require_POST
def update_shopping_list_price_alerts(request):
    user_list = get_or_create_shopping_list(request.user)
    set_shopping_list_price_alerts(user_list, request.POST.get("enabled") == "1")
    if is_ajax_request(request):
        return shopping_list_json_response(
            request,
            user_list,
            message=_("Notification settings saved."),
            enabled=user_list.price_alerts_enabled,
        )
    return redirect("shopping_list")


def shared_shopping_list(request, share_token):
    user_list = get_object_or_404(ShoppingList, share_token=share_token)
    return render(
        request,
        "main/shared_shopping_list.html",
        build_shopping_list_context(request, user_list, editable=False),
    )


def print_shopping_list(request, share_token):
    user_list = get_object_or_404(ShoppingList, share_token=share_token)
    return render(
        request,
        "main/shopping_list_print.html",
        {
            "shopping_list": user_list,
            "plan": build_purchase_plan(user_list),
        },
    )


@login_required
def add_to_shopping_list(request, offer_pk):
    offer = get_object_or_404(
        available_offers().select_related("shop", "category", "product"),
        pk=offer_pk,
    )
    if request.method == "POST":
        item = add_offer_to_shopping_list(request.user, offer)
        if is_ajax_request(request):
            return shopping_list_json_response(
                request,
                item.shopping_list,
                message=_("Product added to the list."),
                offer_id=offer.pk,
                in_list_label=_("In list"),
            )
    return redirect(get_safe_next_url(request, request.POST.get("next") or request.GET.get("next")) or "shopping_list")


@login_required
def remove_from_shopping_list(request, item_pk):
    user_list = get_or_create_shopping_list(request.user)
    if request.method == "POST":
        item = get_object_or_404(ShoppingListItem, pk=item_pk, shopping_list=user_list)
        record_shopping_list_event(
            request.user,
            item.source_offer,
            ShoppingListEvent.EventType.REMOVED,
            item.name,
        )
        detach_group_purchase_membership(item)
        item.delete()
        if is_ajax_request(request):
            return shopping_list_json_response(
                request,
                user_list,
                message=_("Product removed from the list."),
            )
    return redirect("shopping_list")


@login_required
@require_POST
def clear_shopping_list(request):
    user_list = get_or_create_shopping_list(request.user)
    items = list(
        ShoppingListItem.objects.filter(shopping_list__user=request.user).select_related(
            "source_offer",
            "source_offer__shop",
        )
    )
    ShoppingListEvent.objects.bulk_create(
        [
            ShoppingListEvent(
                user=request.user,
                shop=item.source_offer.shop,
                offer=item.source_offer,
                event_type=ShoppingListEvent.EventType.CLEARED,
                item_name=item.name,
            )
            for item in items
        ]
    )
    group_ids = list(
        GroupPurchaseMember.objects.filter(
            shopping_list_item_id__in=[item.pk for item in items]
        ).values_list("group_id", flat=True)
    )
    ShoppingListItem.objects.filter(pk__in=[item.pk for item in items]).delete()
    for group_id in set(group_ids):
        close_group_if_empty(group_id)
    if is_ajax_request(request):
        return shopping_list_json_response(
            request,
            user_list,
            message=_("Shopping list cleared."),
        )
    return redirect("shopping_list")


@login_required
def group_purchase_list(request):
    cleanup_group_purchases()
    groups = (
        active_group_purchases()
        .select_related("offer", "offer__shop", "offer__category", "offer__product")
        .annotate(
            participant_count=Count("members", distinct=True),
            quantity_count=Coalesce(
                Sum("members__quantity"),
                Value(0),
                output_field=IntegerField(),
            ),
        )
        .order_by("-last_activity_at", "offer__original_name", "id")
    )
    page_obj = Paginator(groups, 24).get_page(request.GET.get("page"))
    member_group_ids = set(
        GroupPurchaseMember.objects.filter(
            user=request.user,
            group_id__in=[group.pk for group in page_obj.object_list],
        ).values_list("group_id", flat=True)
    )
    for group in page_obj.object_list:
        group.user_is_member = group.pk in member_group_ids
        group.remaining_quantity = max(group.target_quantity - group.quantity_count, 0)
        group.quantity_reached = group.quantity_count >= group.target_quantity

    return render(
        request,
        "main/group_purchase_list.html",
        {"page_obj": page_obj},
    )


@login_required
@require_POST
def join_group_purchase(request, group_pk):
    cleanup_group_purchases()
    group = get_object_or_404(
        active_group_purchases().select_related("offer", "offer__product"),
        pk=group_pk,
    )
    item = add_offer_to_shopping_list(request.user, group.offer)
    membership = GroupPurchaseMember.objects.filter(
        shopping_list_item=item,
        group=group,
        user=request.user,
    ).first()
    if membership is None:
        return redirect("group_purchase_list")
    return redirect("group_purchase_chat", group_pk=group.pk)


@login_required
def group_purchase_chat(request, group_pk):
    cleanup_group_purchases()
    group = get_object_or_404(
        active_group_purchases().select_related(
            "offer",
            "offer__shop",
            "offer__category",
            "offer__product",
        ),
        pk=group_pk,
    )
    get_object_or_404(GroupPurchaseMember, group=group, user=request.user)

    chat_error = ""
    if request.method == "POST":
        body = request.POST.get("body", "").strip()
        if not body:
            chat_error = _("Enter a message.")
        elif len(body) > GroupPurchaseMessage._meta.get_field("body").max_length:
            chat_error = _("The message cannot be longer than 1000 characters.")
        else:
            GroupPurchaseMessage.objects.create(
                group=group,
                sender=request.user,
                body=body,
            )
            touch_group_purchase(group)
            return redirect("group_purchase_chat", group_pk=group.pk)

    messages = list(
        group.messages.select_related("sender").order_by("-id")[:100]
    )
    messages.reverse()
    participants = list(group.members.select_related("user").order_by("joined_at", "id"))
    quantity_count = sum(member.quantity for member in participants)
    return render(
        request,
        "main/group_purchase_chat.html",
        {
            "group": group,
            "chat_messages": messages,
            "participants": participants,
            "quantity_count": quantity_count,
            "remaining_quantity": max(group.target_quantity - quantity_count, 0),
            "quantity_reached": quantity_count >= group.target_quantity,
            "chat_error": chat_error,
        },
    )


@login_required
@require_GET
def group_purchase_messages(request, group_pk):
    cleanup_group_purchases()
    group = get_object_or_404(GroupPurchase, pk=group_pk, status=GroupPurchase.Status.OPEN)
    get_object_or_404(GroupPurchaseMember, group=group, user=request.user)
    try:
        after_id = max(0, int(request.GET.get("after", "0")))
    except (TypeError, ValueError):
        after_id = 0
    messages = group.messages.filter(pk__gt=after_id).select_related("sender").order_by("id")[:100]
    return JsonResponse(
        {
            "messages": [
                {
                    "id": message.pk,
                    "sender": message.sender.get_short_name() or message.sender.username,
                    "body": message.body,
                    "created_at": timezone.localtime(message.created_at).strftime("%d.%m.%Y %H:%M"),
                    "is_own": message.sender_id == request.user.pk,
                }
                for message in messages
            ]
        }
    )


@login_required
@require_POST
def replace_with_best_offer(request, item_pk):
    item = get_object_or_404(
        ShoppingListItem.objects.select_related(
            "shopping_list",
            "source_offer",
            "source_offer__shop",
            "source_offer__category",
            "source_offer__product",
        ),
        pk=item_pk,
        shopping_list__user=request.user,
    )
    user_list = item.shopping_list
    result = get_best_offer(item)
    replaced = False
    if result.best_offer and result.best_offer.pk != item.source_offer_id and result.potential_saving > 0:
        replace_shopping_list_offer(item, result.best_offer)
        replaced = True
    if is_ajax_request(request):
        return shopping_list_json_response(
            request,
            user_list,
            message=_("Product replaced.") if replaced else _("Shopping list updated."),
        )
    return redirect("shopping_list")


@login_required
@require_POST
def toggle_shopping_list_item(request, item_pk):
    item = get_object_or_404(
        ShoppingListItem,
        pk=item_pk,
        shopping_list__user=request.user,
    )
    item.is_purchased = not item.is_purchased
    item.save(update_fields=["is_purchased"])
    record_shopping_list_event(
        request.user,
        item.source_offer,
        (
            ShoppingListEvent.EventType.PURCHASED
            if item.is_purchased
            else ShoppingListEvent.EventType.UNPURCHASED
        ),
        item.name,
    )
    if is_ajax_request(request):
        return shopping_list_json_response(
            request,
            item.shopping_list,
            message=_("Shopping list updated."),
            is_purchased=item.is_purchased,
        )
    return redirect("shopping_list")


def get_list_offer_ids(user):
    if not user.is_authenticated:
        return set()
    return set(
        ShoppingListItem.objects.filter(shopping_list__user=user).values_list("source_offer_id", flat=True)
    )


def build_shopping_list_context(request, user_list, *, editable):
    shared_url = request.build_absolute_uri(
        reverse("shared_shopping_list", args=[user_list.share_token])
    )
    print_url = request.build_absolute_uri(
        reverse("print_shopping_list", args=[user_list.share_token])
    )
    share_title = _("Tannenberg shopping plan")
    share_message = f"{share_title}\n{shared_url}"
    email_query = urlencode(
        {
            "subject": share_title,
            "body": f"{_('Shopping plan by store')}:\n{shared_url}",
        }
    )
    messenger_share_links = (
        {
            "name": "WhatsApp",
            "url": f"https://wa.me/?{urlencode({'text': share_message})}",
            "opens_new_tab": True,
        },
        {
            "name": "Telegram",
            "url": f"https://t.me/share/url?{urlencode({'url': shared_url, 'text': share_title})}",
            "opens_new_tab": True,
        },
        {
            "name": "Messenger",
            "url": f"fb-messenger://share/?{urlencode({'link': shared_url})}",
            "opens_new_tab": False,
        },
        {
            "name": "Viber",
            "url": f"viber://forward?{urlencode({'text': share_message})}",
            "opens_new_tab": False,
        },
        {
            "name": "SMS / iMessage",
            "url": f"sms:?{urlencode({'body': share_message})}",
            "opens_new_tab": False,
        },
    )
    return {
        "shopping_list": user_list,
        "plan": build_purchase_plan(user_list),
        "editable": editable,
        "shared_url": shared_url,
        "print_url": print_url,
        "email_share_url": f"mailto:?{email_query}",
        "messenger_share_links": messenger_share_links,
    }


def is_ajax_request(request):
    return request.headers.get("x-requested-with") == "XMLHttpRequest"


def shopping_list_json_response(request, user_list, *, message, **extra):
    item_count = user_list.items.count()
    payload = {
        "ok": True,
        "message": message,
        "item_count": item_count,
        **extra,
    }
    if request.headers.get("x-shopping-list-fragment") == "1":
        context = build_shopping_list_context(request, user_list, editable=True)
        payload["shopping_list_html"] = render_to_string(
            "main/includes/shopping_list_live.html",
            context,
            request=request,
        )
    return JsonResponse(payload)


def get_safe_next_url(request, next_url):
    if next_url and url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}):
        return next_url
    return ""
