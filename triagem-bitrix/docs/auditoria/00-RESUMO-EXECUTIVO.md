# Auditoria — Triagem automática de documentos (Bitrix24)

Data: 09/11/2026 · Escopo: cópia local `triagem-codigo-20261109-1552` (40 scripts + 30 arquivos de teste) + `CONTEXTO-TRIAGEM.md`.
Método: 6 frentes paralelas, **somente leitura**, sem chamadas de rede. 82 achados no total, cada um com arquivo:linha nos relatórios 01–06.
Os principais foram reconferidos pelo orquestrador direto no código (marcados ✔).

| Relatório | Frente | Achados |
|---|---|---|
| 01-seguranca.md | Segurança, segredos, LGPD | 14 (3 crít., 6 altas) |
| 02-fase1-loteamento.md | Leitura + montagem de lotes | 13 |
| 03-download-estado.md | Download, disjuntor, persistência, concorrência | 21 |
| 04-fase2-ponte.md | Ponte para a fila do painel + dedup | 12 |
| 05-identificacao-auxiliares.md | Nome/CPF, certidões, planilha, bot do traço | 22 |
| 06-testes-operacao.md | Testes, logs, retenção, systemd | — (operacional) |

---

## 1. O que muda na leitura do CONTEXTO

O diagnóstico do CONTEXTO está certo nos sintomas, mas a auditoria achou **causas diferentes** para três deles:

1. **Os 27 GB de backup não vêm do `triagem_grupo.py`.** ✔ Quem cria um `.bak` por ciclo é `completar_cpf_por_nome.py:141` (roda no `pipeline.sh` a cada 10 min). E ele roda toda vez porque o ciclo principal **regrava o lote inteiro** (`triagem_grupo.py:2869-2870`) e apaga o CPF que o `completar` acabou de preencher — loop: preenche → apaga → preenche → backup. Retenção sozinha estanca o disco, mas não o loop.
2. **O "cursor por chat" é gravado e nunca lido.** Cada ciclo relê ~2.000 mensagens por chat; quando a janela de leitura anda, o começo dela vira lote novo com outro ID. Isso explica boa parte dos **900 lotes vazios**, dos **1.102 BAIXA** e dos **503 descartes** por duplicata (reproduzido offline: 3 kits → 9 lotes, `sim_fase1.py`).
3. **`completar_cpf_por_nome` não é "conservador".** ✔ Busca por LIKE só no primeiro nome, lê só a 1ª página (50 contatos) e, ao achar, força `pronto=True`, `requer_revisao=False`, `motivos_revisao=[]` (`:136-140`). Ou seja: **libera para o painel lotes que estavam em revisão** (incompletos, mistos, suspeita de duplicata) e pode atribuir CPF de homônimo.

---

## 2. Críticos — resolver antes de qualquer melhoria

| # | Achado | Onde | Por que é crítico |
|---|---|---|---|
| C1 | **Senha gov.br/Meu INSS dos clientes em texto puro** ✔ | extraída em `lote_para_job.py:85,114,231-239`; texto bruto em `estado.json` (`triagem_grupo.py:2547`) | Replicada em 1.833 backups (27 GB), `backups/fila-jobs-*`, `fila-jobs.json` e `/tmp/_entradas.json`. Escritório de advocacia guardando credencial de cliente sem cifra e sem descarte. |
| C2 | **Caminho Telegram legado ainda ligado e aberto** ✔ — *rebaixado*: o dono confirmou (09/11) que o robô em uso é o **bot nativo do Bitrix (100003)**, não o Telegram | `config.json` `telegram.habilitado=true`; `telegram_regras.py:45-48` (lista vazia = libera); `telegram_grupo.py:414` não lê a lista aninhada | Inofensivo **se** o processo `telegram_grupo.py` não roda (a coleta confirma). Ação: `habilitado=false`, remover unit/processo, tirar `telegram_grupo.py` da produção e marcar os arquivos `CQA…` como legado (são os áudios que nunca baixam). |
| C3 | **`estado.json` gravado sem trava por 5 scripts** | `reconciliar_estado.py`, `reconciliar_arquivos_disco.py`, `completar_cpf_por_nome.py` (mesmo `.tmp` fixo, sem fsync) | Lost update entre processos; se o JSON corromper, `estado_store.ler_estado` devolve `{}` e o `triagem_grupo` regrava por cima → **perda total do estado**. |
| C4 | **Credencial e CPFs reais no código** | e-mail + senha de cliente em `lote_para_fila.py:435`; ~27 CPFs com DV válido em código/testes | Vaza em qualquer cópia/zip do projeto (como esta). |
| C5 | **`completar_cpf_por_nome` libera revisão e pode errar CPF** ✔ | `completar_cpf_por_nome.py:136-140` | Ver item 1.3. |

## 3. Altos — integridade da produção

- **Lote vai parcial para o painel** (F2-02): arquivo não baixado entra como placeholder; a ponte só recusa se *nada* baixou, e depois de enviado o lote nunca recebe o resto. Pesa agora: disjuntor aberto + 453 não baixados.
- **`pipeline.sh` roda a ponte com `--todos-os-fechamentos`** ✔ (`pipeline.sh:15`): desliga a regra do escritório, sem `--limite` e sem `flock` — lotes em revisão/nome ruído/2 CPFs vão ao painel. (Confirmar no servidor se o timer de 10 min usa esse script.)
- **Dedup por subconjunto que só conta baixados** (F2-03): lote com 2 baixados já enviados + 3 pendentes vira `descartado_duplicata`. Parte dos 503 pode ser kit legítimo perdido.
- **Ponte trava** (F2-04): limite de 4 aplicado antes de checar se há arquivo; 4 lotes sem download no topo travam a fila por até 7 dias. Ordem alfabética põe os `LOTE-SIG` (traço, confiança ALTA) atrás.
- **Disjuntor de download** ✔ (`triagem_grupo.py:2325`): quando abre, bloqueia também o fallback VibeCode; `baixar_pendentes`/`download_svc` ignoram o disjuntor e martelam o Bitrix a ~1,8 req/s; o arquivo nunca é "fechado". Provável razão de estar "aberto desde 13:06".
- **Arquivos somem/corrompem sem erro**: `.parcial` órfão faz o índice ser pulado para sempre; Telegram grava direto no destino e aceita arquivo truncado; dois anexos com mesmo nome no lote viram um só (`triagem_grupo.py:2242-2251`).
- **Retry eterno**: os 83 com erro real são retentados 3×/10 min para sempre; nenhum erro é classificado como permanente; arquivos > 20 MB do Telegram não são checados antes do `getFile`.
- **Webhook Bitrix (provável admin `rest/1/`) mal guardado**: descoberto varrendo arquivos de outros projetos (`triagem_grupo.py:136-141`), lido de `/tmp/b24_webhook.txt` (`download_svc.py:33`, gravável por qualquer usuário), e impresso no log em falha (`triagem_grupo.py:450,452`); `certidoes.py` passa o webhook no argumento do `curl` (visível no `ps`).
- **Tudo como root**, sem `umask`; JS em `/tmp` com nome fixo executado como root; certidões de menores em `/tmp/certidao_*.pdf` nunca apagadas.
- **Certidões**: falha marcada como processada (nunca retenta); queda no meio duplica tarefa; busca pelo nome da criança, pega o 1º contato/negócio sem checar unicidade; foto tratada como PDF.
- **Bot do traço**: timeout após envio → reenvia a cada 8 s → `ERROR_LOOP_DETECTED` cala o bot em todos os chats.
- **Planilha**: escreve na "linha livre" sem reler na hora → pode sobrescrever nome/CPF de outro cliente.

## 4. `fonte_leitura` (webhook × API) — não trocar às cegas

- Config está em `webhook`; README e o comentário do próprio config dizem que webhook é "só diagnóstico". ✔
- Com webhook: sem fallback para a API se falhar, `historico_paginas` é ignorado, e o bot do traço lê pela **API** e anuncia "N arquivos" enquanto o lote é montado pelo **webhook** — kit sem anexo sem aviso.
- Porém a medição "386 × 234" é de 21/09, e o código registra em 21/10 um bug de paginação do webhook. **Refazer a medição** (`--testar-fonte`) antes de decidir.
- `modo_traco = delimitador` está coerente com o bot; os comentários é que estão velhos. Risco: config inválido → código volta em silêncio para `grupo_autor` + `api`.

## 5. Testes

- Suíte completa não roda fora do servidor (31 arquivos com `/opt/triagem-bitrix` fixo). Arquivo a arquivo: 262 ok / 25 falhas / 1 erro de coleta; com o caminho remapeado: **287 de 301 passam**, as 14 restantes leem dados de produção. **Nenhuma falha de lógica.**
- **Nenhum teste cobre os defeitos acima.** Sem teste algum: `baixar_pendentes`, `completar_cpf_por_nome`, `reconciliar_*`, `certidoes`; em `lote_para_fila`, `planejar`/`montar_staging`/dedup.
- Testes leem o `estado.json` de produção (dados pessoais) — trocar por fixtures anonimizadas.
- `nomear_lotes_por_cpf` dá `SystemExit` no import sem webhook; `except Exception` em `lote_para_job.py:401` não pega isso.

---

## 6. Plano recomendado (revisa a ordem do CONTEXTO)

**Fase 0 — hoje, risco baixo, sem mexer em lógica**
1. Desativar o caminho Telegram legado (`telegram.habilitado=false`, parar/remover processo se existir) — o robô em uso é o bot do Bitrix 100003.
2. Retenção dos backups: manter 24 últimos + 1/dia por 14 dias (~570 MB). Apagar os antigos **de forma segura** (contêm senha de cliente). Mesmo tratamento para `backups/fila-jobs-*` e `/tmp/_entradas.json`, `/tmp/certidao_*`.
3. Logrotate para `ponte.log`, `pergunta.log`, `certidoes.log` (`copytruncate`).
4. Tirar `--todos-os-fechamentos` do `pipeline.sh` (ou confirmar que o timer não usa).
5. Mover webhook para arquivo `0600` fora do `/tmp`; parar a varredura de outros projetos; **rotacionar o webhook** e a credencial de `lote_para_fila.py:435`.

**Fase 1 — integridade (1 semana)**
6. Uma trava única (`estado_store`) para todo leitor/gravador do `estado.json`, gravação atômica com fsync, e JSON ilegível = **parar**, nunca "começar do zero". Mesmo lock para lote-fila e ponte (`fila-enviados.json`).
7. Ciclo principal passa a **mesclar** campos do lote em vez de regravar (acaba o loop do CPF e dos backups).
8. `completar_cpf_por_nome`: paginar, exigir 2ª confirmação (telefone/autor), validar DV, **não** limpar `requer_revisao`.
9. Ponte: não enviar lote com download incompleto; dedup só com kit 100% baixado; limite aplicado depois do filtro; ordem por confiança/data.
10. Download: disjuntor respeitado por todos e sem bloquear o fallback; filtro de áudio/vídeo na entrada; erro permanente após N tentativas; `.part` + rename; nomes únicos no lote.

**Fase 2 — origem dos lotes ruins**
11. Usar o cursor de verdade; descartar lote nascido do corte da janela; não gravar lote sem arquivo + sem CPF + sem cabeçalho. Marcar os 900 vazios como `IGNORADO`.
12. Remedir webhook × API e decidir `fonte_leitura`; se ficar no webhook, fallback para API.
13. Retry dirigido dos 83 arquivos.

**Fase 3 — sustentação**
14. `conftest.py` com caminho relativo + fixtures anonimizadas; testes para cada correção acima.
15. Units systemd com `flock`, `User=` não-root, `UMask=0077`, `PrivateTmp=yes`, `OnFailure=` alerta; exit code real nos scripts.
16. Tirar scripts de reparo da produção (`reparo_josilene.py` não é idempotente — 2ª execução move arquivos da Kliciane para a Josilene).

## 7. O que só o servidor confirma

Units systemd reais (qual comando cada timer roda, usuário, locks) · conteúdo de `circuit-download.json` e journal das 13:06 · quantos lotes têm senha no texto · contagem de `.parcial`/0 byte em `lotes/` · quantos dos 503 descartes caíram no caso de subconjunto parcial · quantos lotes já foram liberados por "CPF por nome" · se o processo do Telegram está ativo.

> ⚠️ Os relatórios não contêm valores de segredo nem CPFs, mas citam onde estão. A pasta do código (`triagem-codigo-…`) contém credencial e CPFs reais: trate como confidencial ao compartilhar.
