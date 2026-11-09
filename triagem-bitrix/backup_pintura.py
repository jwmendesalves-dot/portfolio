"""Backup das linhas que a pintura vai tocar (valores + cor atual) — antes de aplicar."""
import json
import datetime as dt

from google.oauth2 import service_account
from googleapiclient.discovery import build

SID = "ID_DA_PLANILHA_EXEMPLO"
ABA = "PASTAS PREVI GERAL"
LINHAS = [9597, 9602, 9603, 9605, 9606, 9611, 9612, 9613, 9614, 9615]

cr = service_account.Credentials.from_service_account_file(
    '/opt/agente-separador/service-account.json',
    scopes=["https://www.googleapis.com/auth/spreadsheets"])
svc = build('sheets', 'v4', credentials=cr)

valores = svc.spreadsheets().values().get(
    spreadsheetId=SID, range=f"'{ABA}'!A1:Q10407").execute().get("values", [])

gid = None
for sh in svc.spreadsheets().get(spreadsheetId=SID, fields="sheets.properties").execute()['sheets']:
    if sh['properties']['title'] == ABA:
        gid = sh['properties']['sheetId']

cores = {}
grade = svc.spreadsheets().get(
    spreadsheetId=SID, includeGridData=True,
    ranges=[f"'{ABA}'!A9597:Q9615"],
    fields="sheets(data(rowData(values(userEnteredFormat(backgroundColor)))))").execute()
linhas_grade = grade['sheets'][0]['data'][0].get('rowData', [])
for i, rd in enumerate(linhas_grade):
    linha = 9597 + i
    cor = None
    for cel in rd.get('values', [])[:17]:
        c = (cel.get('userEnteredFormat') or {}).get('backgroundColor')
        if c:
            cor = c
            break
    cores[linha] = cor

arq = f"/opt/triagem-bitrix/backup-pintura-{dt.date.today().isoformat()}.json"
json.dump({'gid': gid, 'linhas': LINHAS,
           'valores': {str(l): (valores[l - 1] if l - 1 < len(valores) else []) for l in LINHAS},
           'cores_antes': {str(k): v for k, v in cores.items()},
           'total_linhas_planilha': len(valores)},
          open(arq, 'w'), ensure_ascii=False)
print("backup gravado em", arq)
for l in LINHAS:
    v = valores[l - 1] if l - 1 < len(valores) else []
    print(f"  L{l}: cor_antes={cores.get(l)} | {[c[:22] for c in v[:7]]}")
