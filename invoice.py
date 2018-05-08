# -*- coding: utf-8 -*-
# The COPYRIGHT file at the top level of this repository contains the full
# copyright notices and license terms.
import glob
import logging
import os
import re
import unicodedata
from decimal import Decimal
from jinja2 import Environment, FileSystemLoader
from lxml import etree
from operator import attrgetter
from subprocess import Popen, PIPE
from tempfile import NamedTemporaryFile

from trytond.model import ModelView, fields
from trytond.pool import Pool, PoolMeta
from trytond.pyson import Bool, Eval
from trytond.transaction import Transaction
from trytond.wizard import Wizard, StateView, StateTransition, Button

__all__ = ['Invoice', 'InvoiceLine', 'CreditInvoiceStart', 'CreditInvoice',
    'GenerateSignedFacturaeAskPassword', 'GenerateSignedFacturae']

# Get from XSD scheme of Facturae 3.2.1
# http://www.facturae.gob.es/formato/Versiones/Facturaev3_2_1.xml
RECTIFICATIVE_REASON_CODES = [
    ("01", "Invoice number", "Número de la factura"),
    ("02", "Invoice serial number", "Serie de la factura"),
    ("03", "Issue date", "Fecha expedición"),
    ("04", "Name and surnames/Corporate name-Issuer (Sender)",
        "Nombre y apellidos/Razón Social-Emisor"),
    ("05", "Name and surnames/Corporate name-Receiver",
        "Nombre y apellidos/Razón Social-Receptor"),
    ("06", "Issuer's Tax Identification Number",
        "Identificación fiscal Emisor/obligado"),
    ("07", "Receiver's Tax Identification Number",
        "Identificación fiscal Receptor"),
    ("08", "Issuer's address", "Domicilio Emisor/Obligado"),
    ("09", "Receiver's address", "Domicilio Receptor"),
    ("10", "Item line", "Detalle Operación"),
    ("11", "Applicable Tax Rate", "Porcentaje impositivo a aplicar"),
    ("12", "Applicable Tax Amount", "Cuota tributaria a aplicar"),
    ("13", "Applicable Date/Period", "Fecha/Periodo a aplicar"),
    ("14", "Invoice Class", "Clase de factura"),
    ("15", "Legal literals", "Literales legales"),
    ("16", "Taxable Base", "Base imponible"),
    ("80", "Calculation of tax outputs", "Cálculo de cuotas repercutidas"),
    ("81", "Calculation of tax inputs", "Cálculo de cuotas retenidas"),
    ("82",
        "Taxable Base modified due to return of packages and packaging "
        "materials",
        "Base imponible modificada por devolución de envases / embalajes"),
    ("83", "Taxable Base modified due to discounts and rebates",
        "Base imponible modificada por descuentos y bonificaciones"),
    ("84",
        "Taxable Base modified due to firm court ruling or administrative "
        "decision",
        "Base imponible modificada por resolución firme, judicial o "
        "administrativa"),
    ("85",
        "Taxable Base modified due to unpaid outputs where there is a "
        "judgement opening insolvency proceedings",
        "Base imponible modificada cuotas repercutidas no satisfechas. Auto "
        "de declaración de concurso"),
    ]
# UoM Type from UN/CEFACT
UOM_CODE2TYPE = {
    'u': '01',
    'h': '02',
    'kg': '03',
    'g': '21',
    's': '34',
    'm': '25',
    'km': '22',
    'cm': '16',
    'mm': '26',
    'm³': '33',
    'l': '04',
    }
# Missing types in product/uom.xml
# "06", Boxes-BX
# "07", Trays, one layer no cover, plastic-DS
# "08", Barrels-BA
# "09", Jerricans, cylindrical-JY
# "10", Bags-BG
# "11", Carboys, non-protected-CO
# "12", Bottles, non-protected, cylindrical-BO
# "13", Canisters-CI
# "14", Tetra Briks
# "15", Centiliters-CLT
# "17", Bins-BI
# "18", Dozens
# "19", Cases-CS
# "20", Demijohns, non-protected-DJ
# "23", Cans, rectangular-CA
# "24", Bunches-BH
# "27", 6-Packs
# "28", Packages-PK
# "29", Portions
# "30", Rolls-RO
# "31", Envelopes-EN
# "32", Tubs-TB
# "35", Watt-WTT


_slugify_strip_re = re.compile(r'[^\w\s-]')
_slugify_hyphenate_re = re.compile(r'[-\s]+')


def slugify(value):
    if not isinstance(value, unicode):
        value = unicode(value)
    value = unicodedata.normalize('NFKD', value).encode('ascii', 'ignore')
    value = unicode(_slugify_strip_re.sub('', value).strip().lower())
    return _slugify_hyphenate_re.sub('-', value)


def module_path():
    return os.path.dirname(os.path.abspath(__file__))


class Invoice:
    __name__ = 'account.invoice'
    __metaclass__ = PoolMeta
    credited_invoices = fields.Function(fields.One2Many('account.invoice',
            None, 'Credited Invoices'),
        'get_credited_invoices', searcher='search_credited_invoices')
    rectificative_reason_code = fields.Selection(
        [(None, "")] + [(x[0], x[1]) for x in RECTIFICATIVE_REASON_CODES],
        'Rectificative Reason Code', sort=False,
        states={
            'invisible': ~Bool(Eval('credited_invoices')),
            'required': (Bool(Eval('credited_invoices'))
                & (Eval('state').in_(['posted', 'paid']))),
            }, depends=['credited_invoices'])
    invoice_facturae = fields.Binary('Factura-e',
        filename='invoice_facturae_filename', readonly=True)
    invoice_facturae_filename = fields.Function(fields.Char(
            'Factura-e filename'),
        'get_invoice_facturae_filename')

    @classmethod
    def __setup__(cls):
        super(Invoice, cls).__setup__()
        cls._check_modify_exclude.append('invoice_facturae')
        cls._buttons.update({
                'generate_facturae_wizard': {
                    'invisible': ((Eval('type') != 'out')
                        | ~Eval('state').in_(['posted', 'paid'])),
                    'readonly': Bool(Eval('invoice_facturae')),
                    }
                })
        cls._error_messages.update({
                'missing_certificate': (
                    'Missing Factura-e Certificate in company "%s".'),
                'company_facturae_fields': (
                    'Missing some Factura-e fields in company\'s Party "%s".'),
                'company_vat_identifier': (
                    'Missing VAT Identifier in company\'s Party "%s", or its '
                    'length is not between 3 and 30.'),
                'company_address_fields': (
                    'Missing Street, Zip, City, Subdivision or Country in '
                    'default address of company\'s Party "%s".'),
                'party_facturae_fields': (
                    'Missing some Factura-e fields in party "%(party)s" of '
                    'invoice "%(invoice)s".'),
                'party_vat_identifier': (
                    'Missing VAT Identifier in party "%(party)s" of invoice '
                    '"%(invoice)s", or its length is not between 3 and 30.'),
                'party_name_surname': (
                    'The name of "%(party)s" of invoice "%(invoice)s" '
                    'doesn\'t contain the Name and the, at least, First '
                    'Surname.\n'
                    'They must to be separated by one space: Name Surname.'),
                'invoice_address_fields': (
                    'Missing Street, Zip, City, Subdivision or Country in '
                    'Invoice Address of invoice "%s".'),
                'no_rate': ('No rate found for currency "%(currency)s" on '
                    '"%(date)s"'),
                'missing_payment_type': ('The payment type is missing in some '
                    'move lines of invoice "%s".'),
                'missing_payment_type_facturae_type': (
                    'The Factura-e Type is missing in payment type '
                    '"%(payment_type)s" used in invoice "%(invoice)s".'),
                'missing_account_bank_module': (
                    'You must to install "account_bank" module to inform the '
                    'Bank Account in invoices.\n'
                    'The payment type "%(payment_type)s" used in invoice '
                    '"%(invoice)s" is configured as "Direct Debit" or '
                    '"Credit Transfer" and it requires the company/party bank '
                    'account.'),
                'missing_bank_account': ('The Bank Account is missing in some '
                    'move lines of invoice "%s".'),
                'missing_iban': ('The Bank Account "%(bank_account)s" used in '
                    'invoice "%(invoice)s" doesn\'t have an IBAN number and '
                    'it\'s required to generate the Factura-e document.'),
                'invalid_factura_xml_file': (
                    'The Factura-e file (XML) generated for invoice "%s" is '
                    'not valid against the oficial XML Schema Definition.'),
                'error_signing': ('Error signing invoice "%(invoice)s".\n'
                    'Message returned by signing process: %(process_output)s'),
                })

    def get_credited_invoices(self, name):
        pool = Pool()
        InvoiceLine = pool.get('account.invoice.line')
        invoices = set()
        for line in self.lines:
            if isinstance(line.origin, InvoiceLine) and line.origin.invoice:
                invoices.add(line.origin.invoice.id)
        return list(invoices)

    @classmethod
    def search_credited_invoices(cls, name, clause):
        return [('lines.origin.invoice',) + tuple(clause[1:3])
            + ('account.invoice.line',) + tuple(clause[3:])]

    def get_invoice_facturae_filename(self, name):
        return 'facturae-%s.xsig' % slugify(self.number)

    @property
    def rectificative_reason_spanish_description(self):
        if self.rectificative_reason_code:
            for code, _, spanish_description in RECTIFICATIVE_REASON_CODES:
                if code == self.rectificative_reason_code:
                    return spanish_description

    @property
    def taxes_outputs(self):
        """Return list of 'impuestos repecutidos'"""
        return [inv_tax for inv_tax in self.taxes
            if inv_tax.tax and inv_tax.tax.rate >= Decimal(0)]

    @property
    def taxes_withheld(self):
        """Return list of 'impuestos retenidos'"""
        return [inv_tax for inv_tax in self.taxes
            if inv_tax.tax and inv_tax.tax.rate < Decimal(0)]

    @property
    def payment_details(self):
        return sorted([ml for ml in self.move.lines
                if ml.account.kind == 'receivable'],
            key=attrgetter('maturity_date'))

    def _credit(self):
        invoice_vals = super(Invoice, self)._credit()
        rectificative_reason_code = Transaction().context.get(
            'rectificative_reason_code')
        if rectificative_reason_code:
            invoice_vals['rectificative_reason_code'] = (
                rectificative_reason_code)
        return invoice_vals

    @classmethod
    @ModelView.button_action(
        'account_invoice_facturae.wizard_generate_signed_facturae')
    def generate_facturae_wizard(cls, invoices):
        pass

    @classmethod
    def generate_facturae(cls, invoices, certificate_password):
        to_save = []
        for invoice in invoices:
            if invoice.invoice_facturae:
                continue
            facturae_content = invoice.get_facturae()
            invoice._validate_facturae(facturae_content)
            invoice.invoice_facturae = invoice._sign_facturae(
                facturae_content, certificate_password)
            to_save.append(invoice)
        if to_save:
            cls.save(to_save)

    def get_facturae(self):
        """Return the content of factura-e XML file"""
        pool = Pool()
        Currency = pool.get('currency.currency')
        Date = pool.get('ir.date')
        Rate = pool.get('currency.currency.rate')

        if self.type != 'out':
            return
        if self.state not in ('posted', 'paid'):
            return

        # These are an assert because it shouldn't happen
        assert self.invoice_date <= Date.today(), (
            "Invoice date of invoice %s is in the future" % self.id)
        assert len(self.credited_invoices) < 2, (
            "Too much credited invoices for invoice %s" % self.id)
        assert not self.credited_invoices or self.rectificative_reason_code, (
            "Missing rectificative_reason_code for invoice %s with credited "
            "invoices" % self.id)
        assert len(self.taxes_outputs) > 0, (
            "Missing some tax in invoice %s" % self.id)

        if not self.company.facturae_certificate:
            self.raise_user_error('missing_certificate',
                (self.company.rec_name,))

        if (not self.company.party.facturae_person_type
                or not self.company.party.facturae_residence_type):
            self.raise_user_error('company_facturae_fields',
                (self.company.party.rec_name,))
        if (not self.company.party.vat_code
                or len(self.company.party.vat_code) < 3
                or len(self.company.party.vat_code) > 30):
            self.raise_user_error('company_vat_identifier',
                (self.company.party.rec_name,))
        if (not self.company.party.addresses
                or not self.company.party.addresses[0].street
                or not self.company.party.addresses[0].zip
                or not self.company.party.addresses[0].city
                or not self.company.party.addresses[0].subdivision
                or not self.company.party.addresses[0].country):
            self.raise_user_error('company_address_fields',
                (self.company.party.rec_name,))

        if (not self.party.facturae_person_type
                or not self.party.facturae_residence_type):
            self.raise_user_error('party_facturae_fields', {
                    'party': self.party.rec_name,
                    'invoice': self.rec_name,
                    })
        if (not self.party.vat_code
                or len(self.party.vat_code) < 3
                or len(self.party.vat_code) > 30):
            self.raise_user_error('party_vat_identifier', {
                    'party': self.party.rec_name,
                    'invoice': self.rec_name,
                    })
        if (self.party.facturae_person_type == 'F'
                and len(self.party.name.split(' ', 2)) < 2):
            self.raise_user_error('party_name_surname', {
                    'party': self.party.rec_name,
                    'invoice': self.rec_name,
                    })
        if (not self.invoice_address.street
                or not self.invoice_address.zip
                or not self.invoice_address.city
                or not self.invoice_address.subdivision
                or not self.invoice_address.country):
            self.raise_user_error('invoice_address_fields', (self.rec_name,))

        euro, = Currency.search([('code', '=', 'EUR')])
        if self.currency != euro:
            assert (euro.rate == Decimal(1)
                or self.currency.rate == Decimal(1)), (
                "Euro currency or the currency of invoice %s must to be the "
                "base currency" % self.id)
            if euro.rate == Decimal(1):
                rates = Rate.search([
                        ('currency', '=', self.currency),
                        ('date', '<=', self.invoice_date),
                        ], limit=1, order=[('date', 'DESC')])
                if not rates:
                    self.raise_user_error('no_rate', {
                            'currency': self.currenc.name,
                            'date': self.invoice_date.strftime('%d/%m/%Y'),
                            })
                exchange_rate = rates[0].rate
                exchange_rate_date = rates[0].date
            else:
                rates = Rate.search([
                        ('currency', '=', euro),
                        ('date', '<=', self.invoice_date),
                        ], limit=1, order=[('date', 'DESC')])
                if not rates:
                    self.raise_user_error('no_rate', {
                            'currency': euro.name,
                            'date': self.invoice_date.strftime('%d/%m/%Y'),
                            })
                exchange_rate = Decimal(1) / rates[0].rate
                exchange_rate_date = rates[0].date
        else:
            exchange_rate = exchange_rate_date = None

        for invoice_tax in self.taxes:
            assert invoice_tax.tax, 'Empty tax in invoice %s' % self.id
            assert (invoice_tax.tax.type == 'percentage'), (
                'Unsupported non percentage tax %s of invoice %s'
                % (invoice_tax.tax.id, self.id))

        for move_line in self.payment_details:
            if not move_line.payment_type:
                self.raise_user_error('missing_payment_type', (self.rec_name,))
            if not move_line.payment_type.facturae_type:
                self.raise_user_error('missing_payment_type_facturae_type', {
                        'payment_type': move_line.payment_type.rec_name,
                        'invoice': self.rec_name,
                        })
            if move_line.payment_type.facturae_type in ('02', '04'):
                if not hasattr(move_line, 'account_bank'):
                    self.raise_user_error('missing_account_bank_module', {
                        'payment_type': move_line.payment_type.rec_name,
                        'invoice': self.rec_name,
                        })
                if not move_line.bank_account:
                    self.raise_user_error('missing_bank_account',
                        (self.rec_name,))
                if not [n for n in move_line.bank_account.numbers
                        if n.type == 'iban']:
                    self.raise_user_error('missing_iban', {
                        'bank_account': move_line.bank_account.rec_name,
                        'invoice': self.rec_name,
                        })

        jinja_env = Environment(
            loader=FileSystemLoader(module_path()),
            trim_blocks=True,
            lstrip_blocks=True,
            )
        jinja_template = jinja_env.get_template('template_facturae_3.2.1.xml')

        return jinja_template.render({
                'invoice': self,
                'Decimal': Decimal,
                'euro': euro,
                'exchange_rate': exchange_rate,
                'exchange_rate_date': exchange_rate_date,
                'UOM_CODE2TYPE': UOM_CODE2TYPE,
                }, ).encode('utf-8')

    def _validate_facturae(self, xml_string):
        """
        Inspired by https://github.com/pedrobaeza/l10n-spain/blob/d01d049934db55130471e284012be7c860d987eb/l10n_es_facturae/wizard/create_facturae.py
        """
        logger = logging.getLogger('account_invoice_facturae')

        schema_file_path = os.path.join(
            module_path(),
            'Facturaev3_2_1-offline.xsd')
        with open(schema_file_path) as schema_file:
            facturae_schema = etree.XMLSchema(file=schema_file)
            logger.debug("Schema Facturaev3_2_1-offline.xsd loaded")

        try:
            facturae_schema.assertValid(etree.fromstring(xml_string))
            logger.debug("Factura-e XML of invoice %s validated",
                self.rec_name)
        except Exception:
            logger.warning("Error validating generated Factura-e file",
                exc_info=True)
            logger.warning(xml_string)
            self.raise_user_error('invalid_factura_xml_file', (self.rec_name,))
        return True

    def _sign_facturae(self, xml_string, certificate_password):
        """
        Inspired by https://github.com/pedrobaeza/l10n-spain/blob/d01d049934db55130471e284012be7c860d987eb/l10n_es_facturae/wizard/create_facturae.py
        """
        assert self.company.facturae_certificate, (
            'Missing Factura-e certificate in company "%s"' % self.company.id)

        logger = logging.getLogger('account_invoice_facturae')

        unsigned_file = NamedTemporaryFile(suffix='.xml', delete=False)
        unsigned_file.write(xml_string)
        unsigned_file.close()

        cert_file = NamedTemporaryFile(suffix='.pfx', delete=False)
        cert_file.write(self.company.facturae_certificate)
        cert_file.close()

        signed_file = NamedTemporaryFile(suffix='.xsig', delete=False)

        env = {}
        env.update(os.environ)
        libs = os.path.join(module_path(), 'java', 'lib', '*.jar')
        env['CLASSPATH'] = ':'.join(glob.glob(libs))

        # TODO: implement Signer with python
        # http://www.pyopenssl.org/en/stable/api/crypto.html#OpenSSL.crypto.load_pkcs12
        signature_command = [
            'java',
            '-Djava.awt.headless=true',
            'com.nantic.facturae.Signer',
            '0',
            unsigned_file.name,
            signed_file.name,
            'facturae31',
            cert_file.name,
            certificate_password
            ]
        signature_process = Popen(signature_command,
            stdout=PIPE,
            stderr=PIPE,
            env=env,
            cwd=os.path.join(module_path(), 'java'))
        output, err = signature_process.communicate()
        rc = signature_process.returncode
        if rc != 0:
            logger.warning('Error %s signing invoice "%s" with command '
                '"%s <password>": %s %s', rc, self.id,
                signature_command[:-1], output, err)
            self.raise_user_error('error_signing', {
                    'invoice': self.rec_name,
                    'process_output': output,
                    })
        logger.info("Factura-e for invoice %s (%s) generated and signed",
            self.rec_name, self.id)

        signed_file_content = signed_file.read()
        signed_file.close()

        os.unlink(unsigned_file.name)
        os.unlink(cert_file.name)
        os.unlink(signed_file.name)

        return signed_file_content


class InvoiceLine:
    __name__ = 'account.invoice.line'
    __metaclass__ = PoolMeta

    @property
    def taxes_outputs(self):
        """Return list of 'impuestos repecutidos'"""
        return [inv_tax for inv_tax in self.invoice_taxes
            if inv_tax.tax and inv_tax.tax.rate >= Decimal(0)]

    @property
    def taxes_withheld(self):
        """Return list of 'impuestos retenidos'"""
        return [inv_tax for inv_tax in self.invoice_taxes
            if inv_tax.tax and inv_tax.tax.rate < Decimal(0)]

    @property
    def taxes_additional_line_item_information(self):
        res = {}
        for inv_tax in self.invoice_taxes:
            if inv_tax.tax and (not inv_tax.tax.report_type
                    or inv_tax.tax.report_type == '05'):
                key = (inv_tax.tax.rate * 100, inv_tax.base, inv_tax.amount)
                res.setdefault('05', []).append((key, inv_tax.description))
            elif inv_tax.tax and inv_tax.tax.report_description:
                res[inv_tax.tax.report_type] = inv_tax.tax.report_description
        if '05' in res:
            if len(res['05']) == 1:
                res['05'] = res['05'][0]
            else:
                for key, tax_description in res['05']:
                    res['05 %s %s %s' % key] = tax_description
                del res['05']
        return res


class CreditInvoiceStart:
    __name__ = 'account.invoice.credit.start'
    __metaclass__ = PoolMeta
    rectificative_reason_code = fields.Selection(
        [(x[0], x[1]) for x in RECTIFICATIVE_REASON_CODES],
        'Rectificative Reason Code', required=True, sort=False)


class CreditInvoice:
    __name__ = 'account.invoice.credit'
    __metaclass__ = PoolMeta

    def do_credit(self, action):
        with Transaction().set_context(
                rectificative_reason_code=self.start.rectificative_reason_code
                ):
            return super(CreditInvoice, self).do_credit(action)


class GenerateSignedFacturaeAskPassword(ModelView):
    'Generate Signed Factura-e file - Ask Password'
    __name__ = 'account.invoice.generate_facturae.ask_password'
    certificate_password = fields.Char('Certificate Password', required=True)


class GenerateSignedFacturae(Wizard):
    'Generate Signed Factura-e file'
    __name__ = 'account.invoice.generate_facturae'
    start = StateView('account.invoice.generate_facturae.ask_password',
        'account_invoice_facturae.ask_password_view_form', [
            Button('Cancel', 'end', 'tryton-cancel'),
            Button('Generate', 'generate', 'tryton-executable', default=True),
            ])
    generate = StateTransition()

    def transition_generate(self):
        pool = Pool()
        Invoice = pool.get('account.invoice')
        Invoice.generate_facturae(Invoice.browse(
                Transaction().context['active_ids']),
            self.start.certificate_password)
        return 'end'