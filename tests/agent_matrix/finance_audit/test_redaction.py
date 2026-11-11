"""Acceptance #59–60 and PII invariants."""
import hashlib
import logging
import pytest
from agent_matrix.finance_audit.models import PiiType
from agent_matrix.finance_audit.redaction import PiiRedactor, RedactionContext, PiiLoggingFilter
from agent_matrix.finance_audit.errors import PiiLeakError
from tests.agent_matrix.finance_audit.build_data import CARD, CARD2, SPACED_CARD, ID, PHONE
from tests.agent_matrix.finance_audit.conftest import submission

@pytest.mark.parametrize('text,kind',[(CARD,PiiType.BANK_CARD),(SPACED_CARD,PiiType.BANK_CARD),(CARD2,PiiType.BANK_CARD),
    (ID,PiiType.ID_CARD),(ID.lower(),PiiType.ID_CARD),(PHONE,PiiType.MOBILE_PHONE),('138-0013-8000',PiiType.MOBILE_PHONE),
    ('１３８００１３８０００',PiiType.MOBILE_PHONE),('8613800138000',PiiType.MOBILE_PHONE)])
def test_59_identify(text,kind):
    redactor=PiiRedactor(); result=redactor.redact_text('前 '+text+' 后')
    assert len(result.matches)==1 and result.matches[0].pii_type is kind
    assert result.matches[0].start==2 and result.matches[0].end==2+len(text)
    assert text not in result.text
    assert text[-4:] not in result.text
    with pytest.raises(PiiLeakError) as exc: redactor.ensure_clean(text,sink='ticket')
    assert str(exc.value)==f'ticket 出口检测到 {kind.value}'
    assert not redactor.contains_pii(result.text)

@pytest.mark.parametrize('text',['25442000000123456789','91330100MA12345678','110105194912310021','1234567890123456',
    '2026-11-01','13800.00','0.03278688524590164','X13800138000','13800138000Y','2544 2000 0001 2345 6789',''])
def test_59_false_positive(text):
    assert PiiRedactor().redact_text(text).text==text


def test_59_checks_and_indices():
    assert PiiRedactor._luhn_valid(CARD)
    assert not PiiRedactor._luhn_valid(CARD[:-1]+str((int(CARD[-1])+1)%10))
    assert not PiiRedactor._luhn_valid('abc')
    assert PiiRedactor._id_card_valid(ID)
    assert not PiiRedactor._id_card_valid('11010519490231002X')
    assert not PiiRedactor._id_card_valid(ID[:-1]+'1')
    text='ﬀ'+PHONE
    result=PiiRedactor().redact_text(text)
    assert result.matches[0].start==1


async def test_60_numbering_and_determinism(ledger):
    redactor=PiiRedactor(); s=submission(ledger,'E16')
    s=s.model_copy(update={'payee_bank_account':CARD})
    a=redactor.redact_expense(s); b=redactor.redact_expense(s)
    assert a.model_dump_json()==b.model_dump_json()
    assert a.pii_counts=={PiiType.BANK_CARD:1,PiiType.ID_CARD:1,PiiType.MOBILE_PHONE:1}
    assert '[银行卡#1]' in a.reason and '[身份证#1]' in a.reason and '[手机号#1]' in a.reason
    assert '25442000000123456789' in a.lines[0].description
    context=RedactionContext()
    values=redactor.redact_fields({'a':SPACED_CARD,'b':CARD},context=context)
    assert values['a'].text==values['b'].text=='[银行卡#1]'
    assert (PiiType.BANK_CARD,hashlib.sha256(CARD.encode()).hexdigest()) in context.registry
    assert CARD not in repr(context)
    c=submission(ledger,'C07')
    assert redactor.redact_contract(c).model_dump_json()==redactor.redact_contract(c).model_dump_json()
    assert redactor.redact_contract(c).pii_counts=={PiiType.BANK_CARD:1,PiiType.MOBILE_PHONE:1}


def test_60_log_filter(caplog):
    logger=logging.getLogger('finance-test-filter')
    f=PiiLoggingFilter(PiiRedactor()); logger.addFilter(f)
    try:
        with caplog.at_level(logging.INFO):
            logger.info('事由 %s',PHONE)
            try: raise RuntimeError(CARD)
            except RuntimeError: logger.exception('审查错误')
        assert PHONE not in caplog.text and CARD not in caplog.text
        assert '[手机号#1]' in caplog.text
        assert 'exc_info suppressed' in caplog.text and 'Traceback' not in caplog.text
    finally: logger.removeFilter(f)
