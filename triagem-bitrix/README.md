# Triagem de documentos do Bitrix24 — Escritório Exemplo

Sistema que lê os grupos do escritório no Bitrix24 (conectores WhatsApp), separa a
conversa em **lotes por cliente**, baixa os anexos, identifica nome + CPF, deduplica e
entrega o lote na **fila do painel** (`/opt/agente-separador/data/fila-jobs.json`) — a
linha de entrada de toda a produção. Complementa com **certidões de nascimento** (tarefa
no CRM), **pintura da linha do cliente na planilha** e um **bot de traço** no próprio chat.

- Base de produção: `/opt/triagem-bitrix` (`caminhos.BASE`); staging/fila:
  `/opt/agente-separador/data` (`caminhos.SEPARADOR`); segredos: `/etc/triagem-bitrix`
  (`caminhos.SEGREDOS_DIR`, 0600).
- Este README descreve o **comportamento real do código desta branch** (não o desejado).
  Divergências antigas de documentação estão registradas em
  [`docs/auditoria/06-testes-operacao.md`](docs/auditoria/06-testes-operacao.md) (D1–D13).
  Seções históricas (21/09, 24/09, "onde travava") foram removidas: o histórico fica nos
  relatórios de auditoria e em `docs/remediacao/`.

## Como roda (timers e serviços)

| Unidade | Script | Cadência | Log | Trava |
|---|---|---|---|---|
| `triagem-bitrix` (service) | `triagem_grupo.py --loop N` (fase 1) | contínuo (N = **conferir no servidor**; docs antigas diziam 300 e 120) | `triagem.log` (rotativo 2 MB × 5) + journal | `ciclo.lock` (do Python) |
| `triagem-lote-fila` | baixar pendentes → reconciliar → reconciliar file_ids (7 d) → CPF por nome → enfileirar | 10 min | journal | **compartilhada** `$BASE/fila.lock` (lote_para_fila.py, ponte-rodar.sh, deploy/bin/lote-fila.sh) |
| `triagem-bitrix-ponte` | `lote_para_fila.py --enviar --todos --limite 4 --dias 7` | 5 min | `ponte.log` | **compartilhada** (mesma de cima) |
| `triagem-bitrix-certidoes` | `certidoes.py --enviar` | 5 min | `certidoes.log` | `/var/lock/triagem-bitrix-certidoes.lock` |
| `pintar-linha-triagem` | `pintar_linha.py --aplicar` | 2 min | journal | `/var/lock/pintar-linha-triagem.lock` |
| `triagem-bitrix-bot` | `pergunta_traco.py --ativo --loop 8` — **bot nativo Bitrix 100003** | contínuo | `pergunta.log` | `pergunta-traco.lock` (sai 75 se ocupada) |
| `triagem-bitrix-bot-vigia` | `vigia_bot.sh` (mtime do `pergunta.log`) | 2 min | journal | — |
| `triagem-limpeza` | `deploy/bin/podar-backups.py --aplicar --tmp-orfaos` | diário 04:15 | journal | `estado.json.lock` |

As units/timers **propostos** (com hardening `User=triagem`, `UMask=0077`,
`ProtectSystem=strict`, `OnFailure=`) estão em [`deploy/systemd/`](deploy/systemd/) —
o conteúdo real do servidor é coletado por `coleta-triagem.sh` (conferir). Detalhes de
operação: [`docs/remediacao/RUNBOOK-OPERACIONAL.md`](docs/remediacao/RUNBOOK-OPERACIONAL.md).

```bash
systemctl list-timers --all | grep -E "triagem|pintar"
journalctl -u triagem-lote-fila -n 50 --no-pager
python3 triagem_grupo.py --once               # UM ciclo manual (fase 1)
python3 lote_para_fila.py --simular --todos   # plano da ponte, sem gravar
```

## Fonte de leitura e regras (config.json — valores reais)

| chave | valor real | efeito |
|---|---|---|
| `fonte_leitura` | **`webhook`** | lê via `im.dialog.messages.get` (subconjunto: medição 21/09 de 234 × 386 da API VibeCode). Os comentários do config dizem "webhook só diagnóstico", mas produção roda nele — decisão a rever (auditoria D2). |
| `modo_traco` | **`delimitador`** | o traço (`- – — = _`) abre e fecha o bloco; fallback por janela/timeout fora dele. (Docs antigas diziam `grupo_autor` — D3.) |
| `sinal_traco` | 5 caracteres (`- – — = _`) | mensagem inteira precisa ser só traço |
| `timeout_lote_min` / `janela_min_fallback` / `traco_vigencia_min` | 120 / 30 / 30 | fechamento por inatividade/janela |
| `dialog_ids` | 2 grupos (WhatsApp do Bitrix) | cada chat agrupado separadamente |
| `barreira_identidade` | `true` | documento de outra pessoa interrompe o bloco |
| `pergunta_traco.*` | bot_id 100003, 2 chats | **bot do traço nativo do Bitrix** (em uso) |
| `telegram.*` | `habilitado=true`, `chats_permitidos=[]` | **canal LEGADO, desativado em operação** — nenhum timer/unit o executa; o escritório usa o bot 100003. A correção do config (fail-closed/desligar) é proposta pela Frente T |

- **`pronto` = 1 CPF válido + todos os arquivos baixados + lote FECHADO**; `CONFLITO`,
  `SEM_CPF`, `SEM_ARQUIVOS` e mistos vão para revisão (`requer_revisao`).
- **CPF é a âncora, nunca o primeiro nome.** Regra do escritório (24/09) na ponte:
  entra o lote com traço de abertura E fechamento, e também o fechado por janela/timeout
  quando é de um cliente só e limpo (`ponte_criterio.py`).
- Idempotência da ponte: id determinístico `grupo-<LOTE_ID>` + controle
  `fila-enviados.json` — rodar de novo não duplica job nem staging.

## Telegram legado (desativado)

O código `telegram_grupo.py`/`telegram_regras.py` permanece no repositório por
referência, mas o canal está **desativado**: o escritório não usa mais Telegram e o robô
em uso é o bot nativo do Bitrix (100003). Processos do Telegram não devem rodar; os
`file_id` `CQAC…` no estado são legado (não resolvem em `disk.file.get`). Pendências de
limpeza do canal: relatório da Frente T (`docs/remediacao/frentes/T-telegram.md`).

## Arquivos principais

```
triagem_grupo.py     fase 1: leitura → lotes → estado.json (+ testes embutidos: --testar-cpf/--testar-sinais/--testar-fonte)
lote_para_fila.py    fase 2 (ponte): lote pronto → staging → fila do painel (--simular/--enviar/--status)
lote_para_job.py     montagem/validação do job (formato da fila do painel)
ponte_criterio.py    regra do escritório (o que pode ir à fila)
estado_store.py      persistência do estado: escrita atômica 0600+fsync, trava, backups com poda (onda 1)
estado_lote.py       modelo de estados lógicos e transições (onda 1)
seguranca.py         máscara de segredos/CPF, ler_segredo, temporários privados (onda 1)
cpf_util.py          validação/máscara de CPF (onda 1)
rede_bitrix.py       classificação de erro, retry, limitador por host, disjuntor (onda 1)
bitrix_cliente.py    cliente REST único do Bitrix (onda 1.5)
caminhos.py          BASE / SEPARADOR / SEGREDOS_DIR (sobrescrevíveis por env)
certidoes.py         certidão de nascimento → tarefa no CRM (timer de 5 min)
pintar_linha.py      pinta a linha do cliente na planilha (timer de 2 min)
pergunta_traco.py    bot do traço (Bitrix bot 100003)
monitor_lotes.py     vigia de lotes prontos (cron Hermes, 30 min — conferir no servidor)
deploy/              operação: systemd, logrotate, bin/ (podar-backups, verificar-ambiente, lote-fila, alertar-falha), sudoers
```

Estado e dados: `estado.json` (lotes, arquivos, cursores), `lotes/<LOTE-ID>/` (anexos),
`fila-enviados.json` (idempotência da ponte), `backups-estado/` e `backups/` (retidos por
política — ver S05), `triagem.log`/`ponte.log`/`pergunta.log`/`certidoes.log`.
**`vibecode.key` e demais segredos nunca são versionados** (fonte única:
`/etc/triagem-bitrix/`, 0600).

## Operação e infraestrutura (deploy/)

- [`deploy/systemd/`](deploy/systemd/) — units/timers propostos com `flock` coerente
  (lote-fila e ponte compartilham a trava de `fila-enviados.json`), `UMask=0077`,
  `ProtectSystem=strict`, `User=triagem` (migração gradual), `OnFailure` de alerta.
- [`deploy/logrotate/triagem-bitrix`](deploy/logrotate/triagem-bitrix) — rotação diária
  com `copytruncate` (o vigia do bot depende do mtime do `pergunta.log`), compressão, 14
  dias / 20 MB.
- [`deploy/bin/podar-backups.py`](deploy/bin/podar-backups.py) — retenção 24 últimos +
  1/dia × 14 dias; **simulação por padrão**; descarte dos legados (contêm senha de
  cliente) só com `--descarte-seguro --autorizado-por`.
- [`deploy/bin/verificar-ambiente.sh`](deploy/bin/verificar-ambiente.sh) — checagens só
  de leitura: permissões de segredos, donos, disco, locks órfãos, passivo de backups.
- Implantação: [`docs/remediacao/PLANO-IMPLANTACAO.md`](docs/remediacao/PLANO-IMPLANTACAO.md);
  dia a dia: [`docs/remediacao/RUNBOOK-OPERACIONAL.md`](docs/remediacao/RUNBOOK-OPERACIONAL.md);
  arquitetura de operação: [`docs/remediacao/ARQUITETURA-CORRIGIDA.md`](docs/remediacao/ARQUITETURA-CORRIGIDA.md).

## Testes

```bash
.venv/bin/python -m pytest -q              # suíte (baseline: 287/301; 14 dependem de dados de produção)
.venv/bin/python -m pytest -q testes/test_estado_store.py   # persistência (onda 1)
python3 triagem_grupo.py --testar-cpf      # extrator de CPF (teste embutido — ver OP-T5)
python3 triagem_grupo.py --testar-sinais   # traço/modos (teste embutido)
python3 lote_para_fila.py --testar         # tradução da ficha (teste embutido)
```

Os testes embutidos acima não rodam sob `pytest` (achado OP-T5 — migração proposta em
`docs/remediacao/frentes/H-infra.md`). Validação dos scripts de `deploy/`: `bash -n`
(aqui) e `shellcheck`/`systemd-analyze verify` no servidor.

> ⚠️ Dados pessoais: o código legado contém CPFs e credencial reais (auditoria S02);
> a remediação é de outras frentes. Não copie `estado.json`, `lotes/` ou backups para
> exemplos, testes ou documentação.
