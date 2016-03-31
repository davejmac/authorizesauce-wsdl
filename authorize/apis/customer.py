from decimal import Decimal
from six import text_type
from six.moves.urllib.parse import urlencode
from datetime import datetime
from ssl import SSLError

from suds import WebFault
from suds.client import Client
from authorize.data import Address, CreditCard

from authorize.apis.transaction import parse_response
from authorize.exceptions import AuthorizeConnectionError, \
    AuthorizeError, AuthorizeResponseError, AuthorizeInvalidError
from authorize.conf import settings

PROD_URL = 'https://api.authorize.net/soap/v1/Service.asmx?WSDL'
TEST_URL = getattr(settings, 'AUTHORIZE_TEST_URL', 'https://apitest.authorize.net/soap/v1/Service.asmx?WSDL')


class CustomerAPI(object):
    def __init__(self, login_id, transaction_key, debug=True, test=False):
        self.url = TEST_URL if debug else PROD_URL
        self.login_id = login_id
        self.transaction_key = transaction_key
        self.transaction_options = urlencode({
            'x_version': '3.1',
            'x_test_request': 'Y' if test else 'F',
            'x_delim_data': 'TRUE',
            'x_delim_char': ';',
        })

    @property
    def client(self):
        # Lazy instantiation of SOAP client, which hits the WSDL url
        if not hasattr(self, '_client'):
            self._client = Client(self.url)
        return self._client

    @property
    def client_auth(self):
        if not hasattr(self, '_client_auth'):
            self._client_auth = self.client.factory.create(
                'MerchantAuthenticationType')
            self._client_auth.name = self.login_id
            self._client_auth.transactionKey = self.transaction_key
        return self._client_auth

    def _make_call(self, service, *args):
        # Provides standard API call error handling
        method = getattr(self.client.service, service)
        try:
            response = method(self.client_auth, *args)
        except (WebFault, SSLError) as e:
            raise AuthorizeConnectionError('Error contacting SOAP API.')
        if response.resultCode != 'Ok':
            error = response.messages[0][0]
            e = AuthorizeResponseError('%s: %s' % (error.code, error.text))
            e.full_response = {
                'response_code': error.code,
                'response_text': error.text,
            }
            raise e
        return response

    def create_saved_profile(self, internal_id, payments=None, email=None):
        """
        Creates a user profile to which you can attach saved payments.
        Requires an internal_id to uniquely identify this user. If a list of
        saved payments is provided, as generated by create_saved_payment,
        these will be automatically added to the user profile. Returns the
        user profile id.
        """
        profile = self.client.factory.create('CustomerProfileType')
        profile.merchantCustomerId = internal_id
        profile.email = email
        if payments:
            payment_array = self.client.factory.create(
                'ArrayOfCustomerPaymentProfileType')
            payment_array.CustomerPaymentProfileType = payments
            profile.paymentProfiles = payment_array
        response = self._make_call('CreateCustomerProfile', profile, 'none')
        profile_id = response.customerProfileId
        payment_ids = None
        if payments:
            payment_ids = response.customerPaymentProfileIdList[0]
        return profile_id, payment_ids

    @staticmethod
    def _address_to_profile(address, payment_profile):
        if address and address.street:
            payment_profile.billTo.address = address.street
        if address and address.city:
            payment_profile.billTo.city = address.city
        if address and address.state:
            payment_profile.billTo.state = address.state
        if address and address.zip_code:
            payment_profile.billTo.zip = address.zip_code
        if address and address.country:
            payment_profile.billTo.country = address.country
        return payment_profile

    def create_saved_payment(self, credit_card, address=None, profile_id=None):
        """
        Creates a payment profile. If profile_id is provided, this payment
        profile will be created in Authorize.net attached to that profile.
        If it is not provided, the payment profile will be returned and can
        be provided in a list to the create_profile call.
        """
        # Create the basic payment profile with credit card details
        payment_profile = self.client.factory.create(
            'CustomerPaymentProfileType')
        customer_type_enum = self.client.factory.create('CustomerTypeEnum')
        payment_profile.customerType = customer_type_enum.individual
        payment_type = self.client.factory.create('PaymentType')
        credit_card_type = self.client.factory.create('CreditCardType')
        credit_card_type.cardNumber = credit_card.card_number
        credit_card_type.expirationDate = '{0.exp_year}-{0.exp_month:0>2}' \
            .format(credit_card)
        credit_card_type.cardCode = credit_card.cvv
        payment_type.creditCard = credit_card_type
        payment_profile.payment = payment_type

        # Customer billing name and address are optional fields
        if credit_card.first_name:
            payment_profile.billTo.firstName = credit_card.first_name
        if credit_card.last_name:
            payment_profile.billTo.lastName = credit_card.last_name
        payment_profile = self._address_to_profile(address, payment_profile)

        # If a profile id is provided, create saved payment on that profile
        # Otherwise, return an object for a later call to create_saved_profile
        if profile_id:
            response = self._make_call('CreateCustomerPaymentProfile',
                profile_id, payment_profile, 'none')
            return response.customerPaymentProfileId
        else:
            return payment_profile

    def retrieve_saved_payment(self, profile_id, payment_id):
        payment_id = int(payment_id)
        profile = self._make_call(
            'GetCustomerProfile', profile_id).profile
        payment_info = {}
        email = None
        if hasattr(profile, 'email'):
            email = text_type(profile.email)
        payment_info['email'] = email
        saved_payment = None
        for payment in profile.paymentProfiles[0]:
            if payment.customerPaymentProfileId == payment_id:
                saved_payment = payment
                break
        if not saved_payment:
            raise AuthorizeError("Payment ID does not exist for this profile.")
        payment_info['number'] = text_type(
            saved_payment.payment.creditCard.cardNumber)
        data = saved_payment.billTo
        payment_info['first_name'] = text_type(getattr(data, 'firstName', ''))
        payment_info['last_name'] = text_type(getattr(data, 'lastName', ''))
        kwargs = {
            'street': getattr(data, 'address', None),
            'city': getattr(data, 'city', None),
            'state': getattr(data, 'state', None),
            'zip_code': getattr(data, 'zip', None),
            'country': getattr(data, 'country', None)}
        kwargs = dict(
            [(key, text_type(value)) for key, value in kwargs.items() if value])
        payment_info['address'] = Address(**kwargs)
        return payment_info

    def update_saved_payment(self, profile_id, payment_id, **kwargs):
        payment_profile = self.client.factory.create(
            'CustomerPaymentProfileExType')
        customer_type_enum = self.client.factory.create('CustomerTypeEnum')
        payment_profile.customerType = customer_type_enum.individual
        payment_simple_type = self.client.factory.create('PaymentType')
        card_simple_type = self.client.factory.create('CreditCardSimpleType')
        number = kwargs['number']
        # Authorize.net uses this constant to indicate that we want to keep
        # the existing expiration date.
        date = 'XXXX'
        card_simple_type.cardNumber = number
        if kwargs['exp_month'] and kwargs['exp_year']:
            exp = CreditCard.exp_time(kwargs['exp_month'], kwargs['exp_year'])
            if exp <= datetime.now():
                raise AuthorizeInvalidError('This credit card has expired.')
            card_simple_type.expirationDate =\
                '{0}-{1:0>2}'.format(kwargs['exp_year'], kwargs['exp_month'])
        else:
            card_simple_type.expirationDate = date
        payment_simple_type.creditCard = card_simple_type
        payment_profile.payment = payment_simple_type
        payment_profile.payment.creditCard = card_simple_type
        payment_profile.customerPaymentProfileId = payment_id

        if kwargs['first_name']:
            payment_profile.billTo.firstName = kwargs['first_name']
        if kwargs['last_name']:
            payment_profile.billTo.lastName = kwargs['last_name']
        payment_profile = self._address_to_profile(
            kwargs['address'], payment_profile)

        self._make_call(
            'UpdateCustomerPaymentProfile', profile_id,
            payment_profile, 'none')

        if not kwargs['email']:
            return
        profile = self.client.factory.create('CustomerProfileExType')
        profile.email = kwargs['email']
        profile.customerProfileId = profile_id
        self._make_call('UpdateCustomerProfile', profile)

    def delete_saved_profile(self, profile_id):
        self._make_call('DeleteCustomerProfile', profile_id)

    def delete_saved_payment(self, profile_id, payment_id):
        self._make_call('DeleteCustomerPaymentProfile',
            profile_id, payment_id)

    def auth(self, profile_id, payment_id, amount, cvv=None):
        if cvv is not None:
            try:
                int(cvv)
            except ValueError:
                raise AuthorizeInvalidError("CVV Must be a number.")
        transaction = self.client.factory.create('ProfileTransactionType')
        auth = self.client.factory.create('ProfileTransAuthOnlyType')
        amount = Decimal(str(amount)).quantize(Decimal('0.01'))
        auth.amount = str(amount)
        auth.customerProfileId = profile_id
        auth.customerPaymentProfileId = payment_id
        auth.cardCode = cvv
        transaction.profileTransAuthOnly = auth
        response = self._make_call('CreateCustomerProfileTransaction',
            transaction, self.transaction_options)
        return parse_response(response.directResponse)

    def _capture(self, profile_id, payment_id, amount, cvv=None):
        """
        Original capture
        """
        if cvv is not None:
            try:
                int(cvv)
            except ValueError:
                raise AuthorizeInvalidError("CVV Must be a number.")
        transaction = self.client.factory.create('ProfileTransactionType')
        capture = self.client.factory.create('ProfileTransAuthCaptureType')
        amount = Decimal(str(amount)).quantize(Decimal('0.01'))
        capture.amount = str(amount)
        capture.customerProfileId = profile_id
        capture.customerPaymentProfileId = payment_id
        capture.cardCode = cvv
        transaction.profileTransAuthCapture = capture
        response = self._make_call('CreateCustomerProfileTransaction',
            transaction, self.transaction_options)
        return parse_response(response.directResponse)
        
    def capture(self, profile_id, payment_id, amount, cvv=None, invoice_num=''):
        """
        Capture with invoice number
        """
        if cvv is not None:
            try:
                int(cvv)
            except ValueError:
                raise AuthorizeInvalidError("CVV Must be a number.")
        transaction = self.client.factory.create('ProfileTransactionType')
        capture = self.client.factory.create('ProfileTransAuthCaptureType')
        
        if invoice_num:
            order = self.client.factory.create('OrderExType')
            order.invoiceNumber = str(invoice_num).strip()
        else:
            order = None
        amount = Decimal(str(amount)).quantize(Decimal('0.01'))
        capture.amount = str(amount)
        capture.customerProfileId = profile_id
        capture.customerPaymentProfileId = payment_id
        capture.cardCode = cvv
        if order:
            capture.order = order
        transaction.profileTransAuthCapture = capture
        response = self._make_call('CreateCustomerProfileTransaction',
            transaction, self.transaction_options)
        return parse_response(response.directResponse)

    def credit(self, profile_id, payment_id, amount):
        # Creates an "unlinked credit" (as opposed to refunding a previous transaction)
        transaction = self.client.factory.create('ProfileTransactionType')
        credit = self.client.factory.create('ProfileTransRefundType')
        amount = Decimal(str(amount)).quantize(Decimal('0.01'))
        credit.amount = str(amount)
        credit.customerProfileId = profile_id
        credit.customerPaymentProfileId = payment_id
        transaction.profileTransRefund = credit
        response = self._make_call('CreateCustomerProfileTransaction',
            transaction, self.transaction_options)
        return parse_response(response.directResponse)
