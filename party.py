# The COPYRIGHT file at the top level of this repository contains the full
# copyright notices and license terms.
from trytond.model import fields
from trytond.pool import PoolMeta

__all__ = ['Party']


class Party:
    __name__ = 'party.party'
    __metaclass__ = PoolMeta
    facturae_person_type = fields.Selection([
            (None, ''),
            ('J', 'Legal Entity'),
            ('F', 'Individual'),
            ], 'Person Type', sort=False)
    facturae_residence_type = fields.Selection([
            (None, ''),
            ('R', 'Resident in Spain'),
            ('U', 'Resident in other EU country'),
            ('E', 'Foreigner'),
            ], 'Residence Type', sort=False)
