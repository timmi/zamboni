import calendar
import sys
import time
from urllib import urlencode
import urlparse

from django import http
from django.conf import settings
from django.db.transaction import commit_on_success
from django.views.decorators.csrf import csrf_exempt

import bleach
import commonware.log

from addons.decorators import (addon_view_factory, can_be_purchased,
                               has_not_purchased)
import amo
from amo.decorators import json_view, login_required, post_required, write
from amo.helpers import absolutify
from amo.urlresolvers import reverse
from lib.crypto.webpay import (get_uuid, InvalidSender, parse_from_webpay,
                                sign_webpay_jwt)
from mkt.webapps.models import Webapp
from stats.models import ClientData, Contribution

from . import webpay_tasks as tasks
from .views import start_purchase

log = commonware.log.getLogger('z.purchase')
addon_view = addon_view_factory(qs=Webapp.objects.valid)


def make_ext_id(addon_pk):
    """
    Generates a webpay/solitude external ID for this addon's primary key.
    """
    # This namespace is currently necessary because app products
    # are mixed into an application's own in-app products.
    # Maybe we can fix that.
    # Also, we may use various dev/stage servers with the same
    # Bango test API.
    domain = getattr(settings, 'DOMAIN', None)
    if not domain:
        domain = 'marketplace-dev'
    ext_id = domain.split('.')[0]
    return '%s:%s' % (ext_id, addon_pk)


@login_required
@addon_view
@can_be_purchased
@has_not_purchased
@write
@post_required
@json_view
def prepare_pay(request, addon):
    """Prepare a BlueVia JWT to pass into navigator.pay()"""
    amount, currency, uuid_, contrib_for = start_purchase(request, addon)
    log.debug('Storing contrib for uuid: %s' % uuid_)
    Contribution.objects.create(addon_id=addon.id, amount=amount,
                                source=request.REQUEST.get('src', ''),
                                source_locale=request.LANG,
                                uuid=str(uuid_), type=amo.CONTRIB_PENDING,
                                paykey=None, user=request.amo_user,
                                price_tier=addon.premium.price,
                                client_data=ClientData.get_or_create(request))

    # Until atob() supports encoded HTML we are stripping all tags.
    # See bug 831524
    app_summary = bleach.clean(unicode(addon.summary), strip=True, tags=[])

    acct = addon.app_payment_account.payment_account
    seller_uuid =acct.solitude_seller.uuid
    issued_at = calendar.timegm(time.gmtime())
    req = {
        'iss': settings.APP_PURCHASE_KEY,
        'typ': settings.APP_PURCHASE_TYP,
        'aud': settings.APP_PURCHASE_AUD,
        'iat': issued_at,
        'exp': issued_at + 3600,  # expires in 1 hour
        'request': {
            'name': unicode(addon.name),
            'description': app_summary,
            'pricePoint': addon.premium.price.pk,
            'id': make_ext_id(addon.pk),
            'postbackURL': absolutify(reverse('webpay.postback')),
            'chargebackURL': absolutify(reverse('webpay.chargeback')),
            'productData': urlencode({'contrib_uuid': uuid_,
                                      'seller_uuid': seller_uuid,
                                      'addon_id': addon.pk}),
        }
    }

    jwt_ = sign_webpay_jwt(req)
    log.debug('Preparing webpay JWT for addon %s: %s' % (addon, jwt_))
    return {'webpayJWT': jwt_,
            'contribStatusURL': reverse('webpay.pay_status',
                                        args=[addon.app_slug, uuid_])}


@login_required
@addon_view
@write
@json_view
def pay_status(request, addon, contrib_uuid):
    """
    Return JSON dict of {status: complete|incomplete}.

    The status of the payment is only complete when it exists by uuid,
    was purchased by the logged in user, and has been marked paid by the
    JWT postback. After that the UI is free to call app/purchase/record
    to generate a receipt.
    """
    au = request.amo_user
    qs = Contribution.objects.filter(uuid=contrib_uuid,
                                     addon__addonpurchase__user=au,
                                     type=amo.CONTRIB_PURCHASE)
    return {'status': 'complete' if qs.exists() else 'incomplete'}


@csrf_exempt
@write
@post_required
def postback(request):
    """Verify signature and set contribution to paid."""
    signed_jwt = request.read()
    try:
        data = parse_from_webpay(signed_jwt, request.META.get('REMOTE_ADDR'))
    except InvalidSender:
        return http.HttpResponseBadRequest()

    pd = urlparse.parse_qs(data['request']['productData'])
    contrib_uuid = pd['contrib_uuid'][0]
    try:
        contrib = Contribution.objects.get(uuid=contrib_uuid)
    except Contribution.DoesNotExist:
        etype, val, tb = sys.exc_info()
        raise LookupError('JWT (iss:%s, aud:%s) for trans_id %s '
                          'links to contrib %s which doesn\'t exist'
                          % (data['iss'], data['aud'],
                             data['response']['transactionID'],
                             contrib_uuid)), None, tb

    contrib.update(transaction_id=data['response']['transactionID'],
                   type=amo.CONTRIB_PURCHASE)

    tasks.send_purchase_receipt.delay(contrib.pk)
    return http.HttpResponse(data['response']['transactionID'])


@csrf_exempt
@write
@post_required
def chargeback(request):
    """
    Verify signature from and create a refund contribution tied
    to the original transaction.
    """
    raise NotImplementedError
