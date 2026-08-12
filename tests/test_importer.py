"""CSV + OFX import: mapping, debit/credit, date parsing, dedupe."""

from budgetbook import importer


SAMPLE_CSV = """Date,Description,Amount,Category
01/05/2026,Coffee Shop,-4.50,Coffee
01/06/2026,Paycheck,2000.00,Income
01/07/2026,Grocery Mart,-63.20,Groceries
"""

DEBIT_CREDIT_CSV = """Posted,Payee,Debit,Credit
2026-01-08,Electric Co,120.00,
2026-01-09,Interest,,3.15
"""


def _write(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return str(p)


def test_import_csv_mapping_and_dates(db, tmp_path):
    path = _write(tmp_path, "stmt.csv", SAMPLE_CSV)
    res = importer.import_csv(db, path)
    assert res["added"] == 3
    assert res["duplicates"] == 0
    txns = db.list_transactions()
    # date auto-detected from MM/DD/YYYY and normalised to ISO
    assert txns[0]["date"] == "2026-01-05"
    assert txns[0]["payee"] == "Coffee Shop"
    assert txns[0]["category"] == "Coffee"
    assert txns[0]["amount"] == -4.50


def test_import_csv_dedupes_on_reimport(db, tmp_path):
    path = _write(tmp_path, "stmt.csv", SAMPLE_CSV)
    importer.import_csv(db, path)
    res = importer.import_csv(db, path)  # same file again
    assert res["added"] == 0
    assert res["duplicates"] == 3
    assert len(db.list_transactions()) == 3


def test_import_csv_debit_credit(db, tmp_path):
    path = _write(tmp_path, "dc.csv", DEBIT_CREDIT_CSV)
    res = importer.import_csv(db, path)
    assert res["added"] == 2
    by_payee = {t["payee"]: t["amount"] for t in db.list_transactions()}
    assert by_payee["Electric Co"] == -120.00   # debit -> negative
    assert by_payee["Interest"] == 3.15         # credit -> positive


def test_explicit_mapping(db, tmp_path):
    csv_text = "when,who,val\n2026-03-01,Rent Corp,-900\n"
    path = _write(tmp_path, "custom.csv", csv_text)
    mapping = {"date": "when", "payee": "who", "amount": "val"}
    res = importer.import_csv(db, path, mapping=mapping)
    assert res["added"] == 1
    assert db.list_transactions()[0]["payee"] == "Rent Corp"


# A tiny but valid OFX 1.x (SGML) bank statement with two transactions.
SAMPLE_OFX = """OFXHEADER:100
DATA:OFXSGML
VERSION:102
SECURITY:NONE
ENCODING:USASCII
CHARSET:1252
COMPRESSION:NONE
OLDFILEUID:NONE
NEWFILEUID:NONE

<OFX>
<SIGNONMSGSRSV1><SONRS><STATUS><CODE>0<SEVERITY>INFO</STATUS>
<DTSERVER>20260201120000<LANGUAGE>ENG</SONRS></SIGNONMSGSRSV1>
<BANKMSGSRSV1><STMTTRNRS><TRNUID>1<STATUS><CODE>0<SEVERITY>INFO</STATUS>
<STMTRS><CURDEF>USD<BANKACCTFROM><BANKID>123456789<ACCTID>00099<ACCTTYPE>CHECKING</BANKACCTFROM>
<BANKTRANLIST><DTSTART>20260101<DTEND>20260131
<STMTTRN><TRNTYPE>DEBIT<DTPOSTED>20260105120000<TRNAMT>-12.50<FITID>t1<NAME>Coffee Shop<MEMO>latte</STMTTRN>
<STMTTRN><TRNTYPE>CREDIT<DTPOSTED>20260110120000<TRNAMT>2000.00<FITID>t2<NAME>Paycheck<MEMO>salary</STMTTRN>
</BANKTRANLIST>
<LEDGERBAL><BALAMT>1987.50<DTASOF>20260131</LEDGERBAL>
</STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>
"""


def test_import_ofx(db, tmp_path):
    path = _write(tmp_path, "stmt.ofx", SAMPLE_OFX)
    res = importer.import_ofx(db, path)
    assert res["added"] == 2
    txns = db.list_transactions()
    amounts = sorted(t["amount"] for t in txns)
    assert amounts == [-12.50, 2000.00]
    payees = {t["payee"] for t in txns}
    assert "Coffee Shop" in payees
    # re-import dedupes
    res2 = importer.import_ofx(db, path)
    assert res2["added"] == 0
    assert res2["duplicates"] == 2
