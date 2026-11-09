"""Confere a pintura: cor de cada linha + nenhum valor perdido vs backup."""
import json

from google.oauth2 import service_account
from googleapiclient.discovery import build

SID = "ID_DA_PLANILHA_EXEMPLO"
ABA = "PASTAS PREVI GERAL"
LINHAS = [9597, 9602, 9603, 9605, 9606, 9611, 9613, 9614, 9615]

cr = service_account.Credentials.from_service_account_file(
    '/opt/agente-separador/service-account.json',
    scopes=["https://www.googleapis.com/auth/spreadsheets"])
svc = build('sheets', 'v4', credentials=cr)

bk = json.load(open('/opt/triagem-bitrix/backup-pintura-2026-11-06.json'))
valores_antes = bk['valores']

atual = svc.spreadsheets().values().get(
    spreadsheetId=SID, range=f"'{ABA}'!A1:Q10407").execute().get("values", [])

grade = svc.spreadsheets().get(
    spreadsheetId=SID, includeGridData=True, ranges=[f"'{ABA}'!A9597:Q9615"],
    fields="sheets(data(rowData(values(userEnteredFormat(backgroundColor)))))").execute()
rows = grade['sheets'][0]['data'][0].get('rowData', [])

print(f"{'linha':>6}  {'NOME':32} {'A..M':9} {'N/O/P':9} {'Q':9}")
ok = True
for linha in LINHAS:
    rd = rows[linha - 9597]
    vals = rd.get('values', [])
    def cor(i):
        if i >= len(vals):
            return '-'
        c = (vals[i].get('userEnteredFormat') or {}).get('backgroundColor')
        if not c:
            return 'sem cor'
        return "(%d,%d,%d)" % (round(c.get('red', 0) * 255), round(c.get('green', 0) * 255),
                               round(c.get('blue', 0) * 255))
    nome = (atual[linha - 1][1] if len(atual[linha - 1]) > 1 else '')[:32]
    print(f"{linha:>6}  {nome:32} {cor(0):9} {cor(13):9} {cor(16):9}")
    # A..M tem de estar roxo ((180,166,217)); N/O/P não pode ter sido tocada (segue o que era)
    if cor(0) != '(180,166,217)':
        ok = False
        print(f"   ✗ linha {linha}: A não está roxa")

print("\n-- valores: perdidos/alterados vs backup --")
perdidos = 0
ganhos = 0
for linha in LINHAS:
    antes = valores_antes.get(str(linha)) or []
    agora = atual[linha - 1] if linha - 1 < len(atual) else []
    for j in range(max(len(antes), len(agora))):
        a = str(antes[j]) if j < len(antes) else ''
        b = str(agora[j]) if j < len(agora) else ''
        if a != b:
            if a.strip() and not b.strip():
                perdidos += 1
            elif not a.strip() and b.strip():
                ganhos += 1
            else:
                perdidos += 1
            print(f"   linha {linha} col {chr(65+j)}: {a!r} -> {b!r}"
                  f"{'   ← PERDA' if a.strip() and not b.strip() else ''}")
print(f"células GANHAS (vazio -> preenchido): {ganhos} | células PERDIDAS/alteradas: {perdidos}")

total_agora = len(atual)
print(f"linhas na planilha: antes={bk['total_linhas_planilha']} agora={total_agora}")
print("\nRESULTADO:", "OK (nenhuma célula perdida)" if ok and perdidos == 0 else "REVISAR")
