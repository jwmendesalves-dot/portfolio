# Frente T — Canal Telegram legado (S03, TG-01, TG-02, TG-03, C2, E2)

Worktree: `~/triagem-bitrix-mimo`, branch `mimo/auxiliares`.
Contexto do dono: **o escritório NÃO usa mais Telegram**; o robô em uso é o bot nativo do
Bitrix (ID 100003). O código Telegram continua no repositório; o `config.json` está com
`telegram.habilitado=true` (mudança de config é do orquestrador — ver "Pendências").

> **Nota de integração:** a onda 1 já estava mergeada na branch quando esta frente retomou.
> Nenhum fallback `# COMPAT-ONDA1` foi criado: os imports são **diretos** de `seguranca`,
> `rede_bitrix` e `estado_store`. "Pontos COMPAT-ONDA1: **nenhum**".

---

## Resumo por achado

| ID | Status | Onde |
|----|--------|------|
| S03 | **corrigido** | `telegram_regras.py`, `telegram_grupo.py` (6 pontos) |
| TG-01 | **corrigido** | `telegram_regras.py` (formato aninhado real) |
| TG-02 | **corrigido** | `telegram_grupo.py` (memória limitada, sem ressuscitar) |
| TG-03 | **corrigido** | `telegram_grupo.py` (grava antes de confirmar) |
| C2 | **corrigido** (parte minha) / **parcial** (parte em arquivos alheios → pendência) | `telegram_grupo.py` |
| E2 | **corrigido** | `telegram_grupo.py` |

---

## S03 · Telegram aceita arquivo de qualquer pessoa com `chats_permitidos=[]` (CRÍTICA)

- **Status:** corrigido.
- **Arquivos:** `telegram_regras.py`, `telegram_grupo.py`, `testes/test_telegram_autorizacao.py`
  (novo), `testes/test_telegram_regras.py`, `testes/test_telegram_download.py`.
- **O que mudou:** a regra foi invertida para **fechado por padrão**:
  `chats_permitidos` vazio/ausente = **NINGUÉM** autorizado
  (`telegram_regras.chat_permitido`: `return str(chat_id) in chats_permitidos(cfg)`).
  A autorização agora é aplicada nos **seis pontos** exigidos:
  1. **recepção** — `processar()` (filtro por chat; anexos também só são procurados entre
     updates de chats autorizados, `_anexo_por_file_id(updates_ok, …)`);
  2. **download** — `baixar_arquivo_telegram(..., cfg=, chat=)` nega ANTES do `getFile`
     (sem `cfg`/`chat` = NEGADO; erro classificado `Classe.ACESSO`);
  3. **resposta** — `decidir_respostas()` pula chat fora da allowlist (não mexe nem no
     estado `aberto` do chat);
  4. **buffer** — `acumular_buffer()` não bufferiza chat não autorizado;
  5. **gravação** — `mesclar_no_estado(resumo, cfg=)` filtra lotes (`dialog_id` `tg:<chat>`)
     e `dialogos` fora da allowlist; sem `cfg` = nada gravado;
  6. **encaminhamento** — `falar_no_chat(..., cfg=)` recusa o envio para chat não autorizado.
- **Testes:** `testes/test_telegram_autorizacao.py` (13 testes: um por ponto + allowlist
  vazia + formato aninhado x legado); `testes/test_telegram_regras.py::test_lista_vazia_significa_ninguem_autorizado`
  (substituiu o teste antigo `test_lista_vazia_significa_aceitar_todos`, que **codificava o
  defeito** — lista vazia = aceitava todo mundo).
- **Evidência (reproduzindo o defeito no código antigo — antes do patch):**
  ```
  $ .venv/bin/python -m pytest -q --continue-on-collection-errors testes/test_telegram_*.py
  ...
  FAILED testes/test_telegram_autorizacao.py::test_allowlist_vazia_nao_deixa_ninguem_entrar
  FAILED testes/test_telegram_autorizacao.py::test_recepcao_ignora_chat_fora_da_allowlist
  FAILED testes/test_telegram_autorizacao.py::test_gravacao_ignora_lote_de_chat_nao_autorizado
  FAILED testes/test_telegram_autorizacao.py::test_buffer_nao_guarda_chat_nao_autorizado
  FAILED testes/test_telegram_autorizacao.py::test_resposta_nao_vai_para_chat_nao_autorizado
  FAILED testes/test_telegram_autorizacao.py::test_encaminhamento_recusa_chat_nao_autorizado
  FAILED testes/test_telegram_grupo.py::test_chat_fora_da_lista_e_ignorado
  42 failed, 22 passed, 1 error in 0.33s
  ```
- **Evidência (depois do patch):** `testes/test_telegram_autorizacao.py: 13 passed in 0.07s`
  (comando: `.venv/bin/python -m pytest -q testes/test_telegram_autorizacao.py`).
- **Divergência da auditoria:** a sugestão da auditoria
  (`cfg.get("chats_permitidos") or (cfg.get("telegram") or {}).get("chats_permitidos")`)
  mantinha o fail-open para lista vazia e deixava a legada sobrepor a aninhada. Implementado
  mais estrito: a chave **aninhada, quando existe, é soberana** (lista vazia aninhada não cai
  para a legada) e o padrão é **fechado**. A auditoria também citava só recepção e resposta;
  buffer, gravação, download e encaminhamento também foram blindados.

## TG-01 · A lista lida pelo código não é a que está no config (formato aninhado)

- **Status:** corrigido.
- **Arquivos:** `telegram_regras.py` (`chats_permitidos()` nova), `testes/test_telegram_regras.py`,
  e os `CFG`s de `testes/test_telegram_{grupo,respostas,batidas,download}.py` migrados para o
  **formato real do `config.json`** (`{"telegram": {"habilitado": true, "chats_permitidos": [...]}}`).
- **O que mudou:** `chats_permitidos(cfg)` lê `cfg["telegram"]["chats_permitidos"]` (formato
  real) e só cai para o `chats_permitidos` do nível de cima quando o bloco `telegram` não tem
  a chave (compat legada). Confirmado no código: `main()` monta `cfg` a partir de
  `tg.carregar_config()` (chaves de topo) e a lista REAL vive em `config.json → telegram.
  chats_permitidos`; o `chat_permitido` antigo lia `cfg.get("chats_permitidos")` no nível de
  cima, onde a chave nunca existe em produção.
- **Testes:** `test_telegram_regras.py::test_chat_permitido_respeita_a_lista_no_formato_aninhado`,
  `::test_formato_legado_no_nivel_de_cima_ainda_e_aceito`, `::test_chats_permitidos_lista_efetiva`;
  `test_telegram_autorizacao.py::test_allowlist_lida_no_formato_aninhado_real_do_config`.
- **Evidência:** no código antigo, `test_telegram_regras.py` (com o formato aninhado) e
  `test_telegram_grupo.py::test_chat_fora_da_lista_e_ignorado` falhavam (rodada de
  reprodução acima: `42 failed`); depois: `test_telegram_regras: 17 passed`, `test_telegram_grupo: 6 passed`.
- **Divergência da auditoria:** nenhuma relevante (linhas citadas batem; o ponto de leitura
  errado está em `telegram_regras.chat_permitido`, consumido em `telegram_grupo.py`).

## TG-02 · Memória de lotes sem limite e que ressuscita lote apagado

- **Status:** corrigido.
- **Arquivos:** `telegram_grupo.py` (`memoria_de_lotes`, `reforcar_lotes_salvos`, `_podar_memoria`),
  `testes/test_telegram_memoria.py` (novo, 9 testes).
- **O que mudou:**
  · cada entrada da memória ganha marcador interno `_memoria = {gravado_em, confirmado}`
    (o marcador **nunca** vaza para o registro do lote no `estado.json`);
  · **limite**: teto de `MEMORIA_MAX_LOTES = 200` entradas (derruba as mais antigas) e idade
    máxima `MEMORIA_IDADE_MAX_H = 24` h (a memória também é podada em cada gravação);
  · **sem ressuscitar**: `reforcar_lotes_salvos` só reaplica lote JOVEM que **nunca chegou ao
    estado** (`confirmado=False`), não marcado como ignorado (`status in {IGNORADO, DESCARTADO}`
    / `ignorado=True`). Lote que chegou ao estado e depois sumiu = **apagado de propósito** e
    não volta. Entrada legada sem carimbo = idade desconhecida, não ressuscita (e sai na poda);
  · o que for reaplicado é marcado `confirmado=True` (ressuscita UMA vez só).
- **Testes (todos reproduzem o defeito no código antigo):** `test_memoria_de_lotes_tem_limite`,
  `test_memoria_apaga_entradas_muito_antigas`, `test_nao_ressuscita_lote_confirmado_que_foi_apagado`,
  `test_nao_ressuscita_lote_marcado_ignorado`, `test_nao_ressuscita_lote_muito_antigo_nunca_confirmado`,
  `test_nao_ressuscita_entrada_legada_sem_carimbo`, `test_marca_confirmado_depois_de_ressuscitar…`,
  `test_reg_de_memoria_nao_vaza_marcadores_para_o_estado`; `test_ressuscita_lote_recente_que_nunca_chegou_ao_estado`
  garante que o anti-perda legítimo continua.
- **Evidência:** rodada de reprodução no código antigo: `FAILED testes/test_telegram_memoria.py::test_memoria_de_lotes_tem_limite`,
  `…::test_nao_ressuscita_lote_confirmado_que_foi_apagado` (entre outras) em
  `42 failed, 22 passed, 1 error in 0.33s`; depois: `test_telegram_memoria: 9 passed in 0.09s`.
- **Divergência da auditoria:** confirmada (linhas 321-356/463 batem). Detalhe extra implementado:
  entradas legadas sem carimbo são descartadas em vez de ressuscitadas — a auditoria não previa
  a migração de formato.

## TG-03 · "Recebido" enviado antes de o lote ser gravado

- **Status:** corrigido.
- **Arquivos:** `telegram_grupo.py` (`main`), `testes/test_telegram_confirma.py` (novo, 4 testes).
- **O que mudou:** a ordem agora é **gravar → responder**:
  `mesclar_no_estado(resumo, cfg=cfg)` roda **antes** de `falar_no_chat`; se a gravação falhar,
  **nenhuma** resposta sai ("nada de 'Recebido' sem lote gravado") e a rodada retorna código 1
  no `--uma-vez`. O lote é guardado na memória com `confirmado=False` para o
  `reforcar_lotes_salvos` tentar gravar de novo (anti-perda). O processamento do loop ficou
  envolto em `try/except` — exceção de gravação/processamento não derruba mais o processo.
- **Testes:** `test_grava_o_lote_antes_de_confirmar` (ordem `mesclar` antes de `falar`),
  `test_falha_ao_gravar_nao_gera_confirmacao_falsa`, `test_falha_ao_gravar_nao_derruba_o_processo`,
  `test_confirmacao_sai_quando_a_gravacao_dar_certo` (caminho normal preservado).
- **Evidência:** no código antigo (rodada de reprodução): `FAILED testes/test_telegram_confirma.py::test_grava_o_lote_antes_de_confirmar`
  (o `falar` acontecia antes do `mesclar`) e `…::test_falha_ao_gravar_nao_derruba_o_processo`
  (exceção solta derrubava `main`); depois: `test_telegram_confirma: 4 passed in 0.07s`.
- **Divergência da auditoria:** nenhuma (linhas 447-453 batem; o `try` pedido na auditoria foi
  implementado em volta do processamento inteiro, não só do `mesclar`).

## C2 · Sem limite de 20 MB antes do `getFile`; erro não permanente

- **Status:** **corrigido** na parte que é dos meus arquivos (`telegram_grupo.py`);
  **parcial** no conjunto do achado — o martelo "volta a cada 10 min por outro caminho" está em
  `baixar_pendentes.py`/`download_svc.py`, que **não são meus** (ver "Pendências").
- **Arquivos:** `telegram_grupo.py` (`baixar_arquivo_telegram`), `testes/test_telegram_download.py`.
- **O que mudou:**
  · `file_size` é conferido **antes** do `getFile`: acima de `LIMITE_ARQUIVO_TELEGRAM` (20 MB)
    → erro **permanente classificado** com a classificação COMPARTILHADA
    (`rede_bitrix.ErroTamanho` → `Classe.TAMANHO`), com `erro_tipo="permanente"`,
    `erro_classe="TAMANHO"` e `recuperacao` instruindo a recuperação **por outro canal**
    (e-mail/WhatsApp do escritório) ou divisão do arquivo;
  · exatamente 20 MB ainda baixa (teste de fronteira); `file_size` ausente não bloqueia;
  · nenhum id de arquivo é enviado a qualquer API do Bitrix por este módulo (o download usa
    exclusivamente `getFile`/`api.telegram.org/file/…` do Bot API) — e as metas de erro não são
    indexadas em `arquivos_index`;
  · toda mensagem de erro vai por `seguranca.mascarar` antes de log/meta (token/CPF não vazam).
- **Testes:** `test_acima_de_20mb_nao_chama_getFile_e_vira_erro_permanente` (reproduz o defeito:
  no código antigo o `getFile` era chamado e o arquivo "baixava"), `test_exatamente_20mb_ainda_baixa`,
  `test_sem_file_size_nao_e_bloqueado_pelo_limite`.
- **Evidência:** rodada de reprodução (código antigo): `FAILED testes/test_telegram_download.py::test_acima_de_20mb_nao_chama_getFile_e_vira_erro_permanente`;
  depois: `test_telegram_download: 13 passed in 0.12s`.
- **Divergência da auditoria:** o relatório sugere `meta.update(erro=…, erro_tipo="permanente")`;
  implementado com `rede_bitrix.classificar`/`ErroTamanho` (tabela compartilhada), acrescentando
  `erro_classe` e `recuperacao` — mesma semântica, sem mecanismo paralelo de classificação.
  Confirmado no código: `baixar_pendentes.py` manda `{"id": int(fid) if … else fid}` ao
  `disk.file.get.json` (id `CQAC…` cru do Telegram vai ao Bitrix) e `download_svc.py` faz
  `int(file_id)` → `ValueError` — ambos FORA dos meus arquivos (pendência).

## E2 · Download grava direto no destino e reaproveita truncado

- **Status:** corrigido.
- **Arquivos:** `telegram_grupo.py` (`baixar_arquivo_telegram`), `testes/test_telegram_download.py`.
- **O que mudou:** o download agora vai para **arquivo temporário exclusivo**
  (`seguranca.arquivo_temp_privado`, 0600, `O_EXCL|O_NOFOLLOW`) → é escrito **em blocos** com
  `fsync` → **validado** (tamanho > 0 e `== file_size` quando houver; divergência =
  `rede_bitrix.ErroIntegridade` → `Classe.INTEGRIDADE`, **nunca** `baixado=True`) → `sha256`
  em blocos (sem carregar o arquivo na RAM) → `os.replace` para o destino. Falha limpa o
  temporário com `seguranca.remover_seguro` e **não** deixa destino nem `.parcial`. O
  reaproveitamento também mudou: só reaproveita arquivo **completo** (tamanho > 0 e `== file_size`);
  truncado existente é rebaixado.
- **Testes:** `test_arquivo_truncado_nao_e_aceito_como_baixado`, `test_download_vazio_nao_vira_baixado`,
  `test_queda_no_meio_nao_deixa_parcial_nem_destino`, `test_nao_reaproveita_arquivo_truncado_existente`,
  `test_tamanho_divergente_e_erro_de_integridade` (todos reproduzem o defeito antigo) +
  caminho normal preservado (`test_baixa_o_arquivo_e_confere_o_sha256`, `test_reaproveita_o_que_ja_existe`).
- **Evidência:** rodada de reprodução (código antigo): `FAILED testes/test_telegram_download.py::test_tamanho_divergente_e_erro_de_integridade`
  (antigo marcava `baixado=True` com só `aviso`), `…::test_nao_reaproveita_arquivo_truncado_existente`,
  `…::test_queda_no_meio_nao_deixa_parcial_nem_destino` (antigo gravava direto no destino);
  depois: `test_telegram_download: 13 passed in 0.12s`.
- **Divergência da auditoria:** nenhuma relevante; a auditoria citava também o custo de RAM de
  `r.read()`/`open().read()` — resolvido (leitura e hash em blocos).

---

## Exigência 7 — gravação de estado só via `estado_store`

`mesclar_no_estado` e `reforcar_lotes_salvos` já usavam `estado_store.atualizar_estado` e
continuam assim. O `telegram-estado.json` (cache deste módulo) deixou de usar escrita própria:
`gravar_estado` → `estado_store.gravar_atomico` e `carregar_estado` → `estado_store.ler_estado`
(corrompido: o `estado_store` preserva a cópia `.corrompido-<ts>` e o cache recomeça). Nenhum
módulo paralelo de máscara/CPF/trava/classificação foi criado: uso de `seguranca.mascarar`,
`seguranca.arquivo_temp_privado`/`remover_seguro` e `rede_bitrix.classificar`/`ErroTamanho`/
`ErroIntegridade`/`FalhaBitrix`.

## Testes — resultado REAL da frente

```
$ .venv/bin/python -m pytest -q testes/test_telegram_grupo.py testes/test_telegram_regras.py \
    testes/test_telegram_download.py testes/test_telegram_respostas.py testes/test_telegram_batidas.py \
    testes/test_telegram_autorizacao.py testes/test_telegram_memoria.py testes/test_telegram_confirma.py \
    testes/test_telegram_desligado.py
81 passed in 0.28s
```

Por arquivo (comando `.venv/bin/python -m pytest -q testes/<arquivo>`):

| arquivo | resultado |
|---|---|
| test_telegram_regras.py | 17 passed in 0.03s |
| test_telegram_grupo.py | 6 passed in 0.11s |
| test_telegram_download.py | 13 passed in 0.12s |
| test_telegram_respostas.py | 9 passed in 0.07s |
| test_telegram_batidas.py | 6 passed in 0.08s |
| test_telegram_autorizacao.py (novo) | 13 passed in 0.07s |
| test_telegram_memoria.py (novo) | 9 passed in 0.09s |
| test_telegram_confirma.py (novo) | 4 passed in 0.07s |
| test_telegram_desligado.py (novo) | 4 passed in 0.07s |

Reprodução dos defeitos no código ANTES do patch (testes novos/atualizados contra o código antigo):
`42 failed, 22 passed, 1 error in 0.33s` (cada achado tem os testes citados acima falhando).

Suíte inteira após as mudanças:
```
$ .venv/bin/python -m pytest -q
19 failed, 778 passed in 7.32s
```
As 19 falhas **não são da Frente T** (nenhum dos arquivos falhos importa módulo Telegram):
`test_nome_regras` (1), `test_pasta_fichinha` (1), `test_pintar_linha_pl03` (3),
`test_pintar_linha_planilha` (2), `test_ponte_fila` (1), `test_servico_lote` (3),
`test_telefones_fichinha` (1), `test_variaveis_contrato` (7) — dependem de dados de produção
ou são das frentes X/H em andamento. O `testes/test_estado_store.py` (dono: frente B) continua
verde: `37 passed in 0.63s` (importa `telegram_grupo`).

## Arquivos alterados/criados (exclusivos da Frente T)

- `telegram_grupo.py` (alterado)
- `telegram_regras.py` (alterado)
- `testes/test_telegram_grupo.py`, `testes/test_telegram_regras.py`, `testes/test_telegram_download.py`,
  `testes/test_telegram_respostas.py`, `testes/test_telegram_batidas.py` (alterados)
- `testes/test_telegram_autorizacao.py`, `testes/test_telegram_memoria.py`,
  `testes/test_telegram_confirma.py`, `testes/test_telegram_desligado.py` (novos)
- `docs/remediacao/frentes/T-telegram.md` (este relatório)

## Pendências para o orquestrador

1. **`config.json` (seu):** propor `telegram.habilitado = false` (canal legado desligado) —
   com a chave ausente/falsa o processo já sai com código 0 sem chamar a API. Manter
   `telegram.chats_permitidos` como está: com o fail-closed, `[]` = ninguém entra (não é mais
   "tudo liberado"). Exemplo de bloco:
   ```json
   "telegram": { "habilitado": false, "chats_permitidos": [] }
   ```
2. **`baixar_pendentes.py` (alheio, parte do C2):** pular `fid` **não numérico** (id `CQAC…`
   do Telegram nunca pode ir ao `disk.file.get` do Bitrix — hoje `{"id": int(fid) if str(fid)
   .strip().isdigit() else fid}` manda o id cru) e pular metas com `erro_tipo == "permanente"`.
   Enquanto isso não sai, lotes do Telegram com arquivo não baixado continuam gerando 3 erros
   por arquivo a cada 10 min.
3. **`download_svc.py:114` (alheio, parte do C2):** id não numérico deve virar falha permanente
   classificada **sem chamada de rede** (hoje `int(file_id)` → `ValueError`).
4. **S03 (opcional, alheio):** a auditoria sugere `requer_revisao=True` na ponte para material
   do Telegram; com a allowlist fechada por padrão, nenhum material do Telegram entra mais —
   decisão sua se ainda vale marcar.
5. **Commit:** nada foi commitado por esta frente (git de escrita é do orquestrador). Sugestão
   de agrupamento (mensagens em pt-BR):
   - `telegram: S03/TG-01 autorização fechada por padrão no formato aninhado real`
     (`telegram_regras.py`, `testes/test_telegram_regras.py`, `testes/test_telegram_autorizacao.py`,
     CFGs de `testes/test_telegram_{grupo,respostas,batidas}.py`)
   - `telegram: canal desabilitado sai com código 0 sem chamar a API`
     (`telegram_grupo.py` main, `testes/test_telegram_desligado.py`)
   - `telegram: TG-02 memória de lotes com limite e sem ressuscitar lote apagado/ignorado`
     (`telegram_grupo.py`, `testes/test_telegram_memoria.py`)
   - `telegram: TG-03 persistir o lote antes de confirmar "Recebido" (sem confirmação falsa)`
     (`telegram_grupo.py` main, `testes/test_telegram_confirma.py`)
   - `telegram: C2/E2 limite de 20 MB antes do getFile e download atômico validado`
     (`telegram_grupo.py` download, `testes/test_telegram_download.py`)
   - `telegram: relatório da frente T` (`docs/remediacao/frentes/T-telegram.md`)

## Pontos `COMPAT-ONDA1`

**Nenhum.** A onda 1 já estava integrada na branch quando o trabalho começou; todos os
consumos são import direto: `seguranca.mascarar`/`arquivo_temp_privado`/`remover_seguro`,
`rede_bitrix.classificar`/`ErroTamanho`/`ErroIntegridade`/`FalhaBitrix`/`Classe`,
`estado_store.ler_estado`/`gravar_atomico`/`atualizar_estado`.

## Precisa do servidor

- Verificar se o processo legado está rodando (`systemctl`/`ps` de `telegram_grupo.py --loop`)
  e **parar/desabilitar** o serviço (S03: a exposição é imediata enquanto ele rodar). No
  servidor atual, o robô ativo é o bot nativo do Bitrix (ID 100003).
- Limpeza dos 3 arquivos >20 MB legados no `estado.json` de produção (marcar
  `erro_tipo="permanente"`/tirar de `file_ids`), conforme a auditoria C2 item 4.
- Conferir permissão 0600 do arquivo de token (`telegram.token_file` /
  `TELEGRAM_TOKEN_FILE`) — não alterado por esta frente.
- `telegram-estado.json` de produção: entradas antigas de `lotes_memoria` no formato legado
  (sem `_memoria`) são ignoradas e apodrecem sozinhas na primeira poda; nada a fazer manual.

## Riscos

- **Comportamento novo intencional:** com `telegram.habilitado=true` e `chats_permitidos=[]`
  (o config de hoje), o canal não aceita ninguém — é o desejado (o escritório não usa
  Telegram), mas quem reativar o canal terá de preencher a allowlist.
- **`erro_tipo="permanente"` para download truncado/divergente** (tabela compartilhada,
  `Classe.INTEGRIDADE`): não há retry automático desses arquivos (o offset do Telegram também
  impede reentrega). Se um truncado for transitório, o redownload é manual.
- Lote que falha ao gravar fica só na memória até o `reforcar_lotes_salvos` regravar (mesma
  rodada); se o processo morrer antes, o lote reaparece na próxima rodada pelo anti-perda
  (janela de 24 h) — e nunca é ressuscitado depois de confirmado.
- `mesclar_no_estado`/`falar_no_chat`/`baixar_arquivo_telegram` **negam** sem `cfg` com
  allowlist (fail-closed): qualquer chamador novo precisa passar o `cfg`.
- Erros agora passam por `seguranca.mascarar` em log/meta: padrões atípicos de segredo podem
  ficar parcialmente mascarados (desejável), sem perda de diagnóstico por classe/tipo.
- Enquanto `baixar_pendentes.py` não pular ids não numéricos (pendência 2), lotes do Telegram
  com arquivo pendente continuam batendo no `disk.file.get` do portal a cada rodada.
