# Frente G (onda 4): cenário integral de ponta a ponta (offline)

Status: **CONCLUÍDO**. O cenário passa, e dois defeitos reais estão documentados com `xfail(strict=True)`.

Arquivos desta frente: `testes/test_cenario_integral.py` (novo) e este relatório. Nenhum código de produção foi editado.

## Cadeia exercitada (a mesma ordem do timer `triagem-lote-fila` + ciclo da triagem)
1. `triagem_grupo.ciclo`: fonte falsa em `bitrix_cliente._post` para `im.dialog.messages.get`, usando a semântica FIRST_ID/LAST_ID do `FonteFalsa` de `test_fase1_cursor`. Fonte `webhook`, modo `delimitador`.
2. `baixar_pendentes.main` → `download_svc`: `disk.file.get` falso no mesmo `_post` e `urllib.request.urlopen` falso para a DOWNLOAD_URL. Os bytes são sintéticos e a VibeCode responde sempre 404.
3. `completar_cpf_por_nome.rodar(aplicar=True)`: CRM falso (`crm.contact.list` e `crm.duplicate.findbycomm`) no mesmo `_post`.
4. `lote_para_fila.main`: `--simular` e depois `--enviar` duas vezes, com o `NODE_FALSO` (FilaJobs) de `test_ponte_rodada.py`.

Tudo roda em `tmp_path`: `TRIAGEM_BASE`, `AGENTE_SEPARADOR_BASE`, `ESTADO_JSON` e `DOWNLOAD_FILA_JSON`, mais as constantes da ponte apontadas para o tmp. CPFs vêm de `cpf_util.gerar_para_teste(501..506)` e os nomes são fictícios.

## Cenário (uma conversa, 3 ciclos + falha + reinício + ciclo extra)
| Kit | Conteúdo | Esperado e verificado |
|---|---|---|
| K0 Maria | CPF válido; dois anexos `RG.pdf` com o mesmo nome; conversa entre documentos; `Senha gov.br` no cabeçalho | enfileirado no ciclo 1; destinos distintos; staging com exatamente os 3 sha256; senha ausente de `estado.json`, stdout e log |
| K1 Joana | CPF com DV inválido | `requer_revisao`, sem CPF válido, fora da fila (o CRM tem 1 homônimo, mas não houve 2ª evidência) |
| K2 Ana | sem CPF; **dois homônimos** no CRM | `completar_cpf` → ambíguo, sem CPF, em revisão, fora da fila |
| K3 Bea | `402` com 503 QUERY_LIMIT na 1ª rodada | ciclo 1: pendente (fila de download `pendente`) e kit **não** enfileirado; depois baixa e o kit vai |
| K4 Clara | `502` com ACCESS_DENIED sempre | `download_svc` marca permanente; kit nunca vai |
| K5 Maria | **reenvio idêntico** de K0 (file_ids novos, mesmos bytes) | `descartado_duplicata`, só depois de 100% baixado |
| K6 Maria | **documento complementar novo** + **áudio no meio do kit** | não é descartado; o áudio é registrado como mídia, sem GET e sem sha256 |
| Aberto | documento + cabeçalho sem o traço de fechamento (recente) | não vira lote no estado (fica em `f1_regiao`) e não vai à fila |
| — | conversa solta entre kits | não vira lote |

Passos: ciclo 1 (K0–K3) → download → ponte → ciclo 2 (K4, K5) → download → ciclo 3 com **falha de gravação simulada** (`estado_store._gravar_bytes` levanta `OSError(28)` na 2ª transação, a dos cursores) → **reinício** (caches de memória zerados, `_webhook_cache` e migração do circuito; tudo relido do disco) → ciclo 3 de novo → download → ciclo 4 sem mensagem nova → `completar_cpf` → ponte `--simular` → `--enviar` ×2.

"Tempo passou" entre os ciclos: os `circuit-*.json` são apagados e o `proxima_em` dos itens pendentes vai para 0. O teste não espera minutos reais.

## Invariantes provados (asserções)
- **Lotes:** 4 → 6 → 7, os ids de cada ciclo continuam no seguinte (`ids1 ⊆ ids2 ⊆ ids3`) e não há lote artificial. O ciclo extra tem `mensagens_novas == 0`.
- **Nenhum documento perdido nem trocado:** para cada file_id, o `sha256` no estado é igual ao do arquivo no disco e ao dos bytes originais (`_conferir_documentos`).
- O áudio não vira documento e nunca é baixado. Kit incompleto (K3 no ciclo 1, K4) não é enfileirado. K5 é descartado só depois de completo. K6 não é descartado.
- **Ponte idempotente:** a 2ª rodada `--enviar` deixa `fila-jobs.json` idêntico e não há job duplicado. `--simular` não altera a fila nem o estado.
- **Falha de gravação:** `estado.json` continua JSON válido, o cursor não anda e nenhum tmp sobra. O ciclo seguinte recupera sem duplicar lote.
- **Cursor coerente:** `cursores[chat1]` e `cursor_msg_id` terminam igual ao id da última mensagem; depois do ciclo 1, o cursor é igual ao último id visível.
- **Segredos:** nenhum CPF (cru ou formatado, inclusive o inválido), token do webhook ou senha aparece em stdout ou `caplog` (nível DEBUG). A senha também não aparece em `estado.json`.

## Defeitos encontrados (xfail strict; viram XPASS e quebram quando corrigidos)
### G-01 · ALTA · `triagem_grupo.py:ciclo` → `_lote_recente_dl` (≈ linha 3406)
- **O que acontece:** `lote["fim"]` vem de `_acumular` (`lote["fim"] = msg["ts"]`) como `datetime`, mas `_parse_data` faz `(s or "").replace("Z", ...)` e só aceita `str`. Com `datetime` ele cai no `except` e devolve `None`. Resultado: `_lote_recente_dl` é sempre `False` e **todo lote vira "adiado (lote antigo / teto do ciclo)"**. O download dentro do ciclo (C1/E3/D3) é código morto em produção desde 27/10. O defeito está no commit inicial e no HEAD; quem baixa de fato é só o `baixar_pendentes` do timer de 10 min, com até 10 min de atraso por kit.
- **Reprodução:** `test_g01_ciclo_baixa_kit_recente`. Também em linha de comando: `python -c "import triagem_grupo as tg, datetime as dt; print(tg._parse_data(dt.datetime.now(dt.timezone.utc)))"` → `None`.
- **Correção sugerida:** em `_lote_recente_dl`, aceitar `datetime` (`ts = v if isinstance(v, _dt.datetime) else _parse_data(v)`), ou fazer o próprio `_parse_data` devolver o `datetime` recebido. Os testes de C1 chamam `baixar_file_id_api` direto e por isso não pegaram o defeito.

### G-02 · ALTA · `download_svc._aplicar_no_estado` × `ponte_criterio.tipo_arquivo`
- **O que acontece:** com a fonte `webhook` (a de produção), a leitura não traz nome de anexo. O `download_svc` descobre que é áudio pelo `NAME` do `disk.file.get` e grava no lote só `erro_tipo="midia"`, sem `tipo` e sem `nome_original`. O `ponte_criterio.tipo_arquivo` lê `tipo` ou o nome, não lê `erro_tipo`, e devolve `"desconhecido"` não baixado. **Qualquer kit com áudio fica `AGUARDANDO_DOWNLOAD` para sempre.** O `baixar_pendentes` não volta a esse item porque ele é permanente para o download.
- **Reprodução:** `test_g02_kit_com_audio_vai_a_fila`, com a mesma cadeia e o áudio em K0. O teste confere `erro_tipo == "midia"` e mostra que o job não sai.
- **Correção sugerida (qualquer uma das duas):** (a) `ponte_criterio.tipo_arquivo` trata `erro_tipo == "midia"` como `midia`; (b) `download_svc._aplicar_no_estado` grava também `tipo="midia"` e `nome_original` (campos do dono `download`). Recomendo as duas: a (a) é defensiva para os lotes que já estão gravados.
- No cenário principal o áudio fica em K6, para que o resto dos invariantes de K0 seja provado.

### Observação (sem teste próprio) · G-03 · MÉDIA
`triagem_grupo._CLASSES_PERMANENTES_ITEM` não inclui `rb.Classe.ACESSO`. No ciclo, um ACCESS_DENIED de item vira `erro_tipo="transitorio"`, vai para a fila de retry e é tentado de novo a cada ciclo, enquanto o `download_svc` (frente D) o trata como permanente. Hoje não tem efeito por causa do G-01; passa a ter assim que o G-01 for corrigido. Sugestão: tratar ACESSO sem `pausa_rodada` (403 de item) como permanente de item, alinhado com a frente D.

## Comportamentos registrados (não são defeitos)
- O lote aberto no fim **não é persistido** em `lotes`: fica só na região provisória `f1_regiao` (C2a). O teste aceita os dois modelos, mas exige que as mensagens não se percam.
- O 503 QUERY_LIMIT interrompe a rodada do `download_svc` (`pausa_rodada`); o item fica `pendente`, com backoff.
- `estado_store` registra em log `campo de lote sem dono declarado: 'cpfs_invalidos'`. Pendência para a frente B/Z2: declarar o campo em `PROPRIEDADE_CAMPOS`.

## Evidência (saídas reais)
```
$ .venv/bin/python -m pytest testes/test_cenario_integral.py -rA
PASSED testes/test_cenario_integral.py::test_cenario_integral_tres_ciclos_reinicio_falha_de_gravacao_e_ponte
XFAIL testes/test_cenario_integral.py::test_g01_ciclo_baixa_kit_recente - G-01: ...
XFAIL testes/test_cenario_integral.py::test_g02_kit_com_audio_vai_a_fila - G-02: ...
========================= 1 passed, 2 xfailed in 0.47s =========================

$ .venv/bin/python -m pytest -q
FAILED testes/test_rede_bitrix.py::test_limitador_compartilhado_entre_processos_respeita_taxa_e_rajada
1 failed, 1092 passed, 2 skipped, 3 xfailed in 15.24s
$ .venv/bin/python -m pytest testes/test_rede_bitrix.py::test_limitador_compartilhado_entre_processos_respeita_taxa_e_rajada
1 passed in 1.44s
```
A falha de `test_rede_bitrix` depende de tempo (limitador entre processos) e passa isolada; ela não é desta frente. A suíte cresceu de 1078 para 1092 passed porque outras frentes da onda acrescentaram testes; durante esta frente havia edição não comitada em `triagem_grupo.py` e o relatório C3 novo, de outra frente.

## O que NÃO foi testado
Rede e servidor reais; `FilaJobs` real (o node é falso); `pipeline.sh`/systemd; o bot do traço; leitura pela API VibeCode (só a fonte webhook); reinício com recarga real de módulo (o "reinício" aqui é estado relido do disco com os caches zerados).

## Pendências para outras frentes
- **Dono de `triagem_grupo.py`:** G-01 e G-03.
- **Donos de `ponte_criterio.py`/`download_svc.py` (E/D):** G-02.
- **B/Z2:** declarar `cpfs_invalidos` em `PROPRIEDADE_CAMPOS`.
