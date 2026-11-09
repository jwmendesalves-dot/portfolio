# Plano de recuperação de dados históricos — triagem-bitrix

> Frente: **Analista / OpenBot (TAREFA 3)** · Branch `openbot/apoio` · base: relatórios `docs/auditoria/0*.md`, `docs/remediacao/frentes/*.md` e código pós-onda 1.
> Os números citados são **medições históricas de 09/11/2026** (CONTEXTO §6) e servem de referência de ordem de grandeza — **não são meta** nem se repetem no servidor hoje. Toda decisão de aplicação usa a medição do dia, feita na Etapa B.

---

## 1. Conclusão primeiro

1. **Nada aqui se resolve com um `UPDATE` em massa.** O estado tem quatro escritores concorrentes e um ciclo que regrava o lote inteiro; uma correção em massa aplicada antes do deploy da onda 1 é apagada no ciclo seguinte (A3) ou perdida por escrita sem trava (A1/A2). **Pré-requisito de todas as fases de escrita: onda 1 implantada e o ciclo rodando com `estado_store` + merge por campo.**
2. **O que é recuperável com baixo risco:** marcar lotes vazios como `IGNORADO` (R1), devolver a revisão lotes liberados por CPF-por-nome (R2) e reclassificar descartes por duplicata com download incompleto (R3). São **marcações reversíveis**, sem apagar dado.
3. **O que exige rede e orçamento:** os 453 não baixados / 83 com erro real (R4). É retry dirigido, lote a lote, com disjuntor e teto de requisições — nunca "baixar tudo de novo".
4. **O que não se recupera sozinho:** documento perdido por colisão de nome (E3) ou por `.parcial` órfão (E1) que já não está no portal. Aí é rebaixamento controlado + pedido ao cliente; o plano diz como detectar e como registrar, não como inventar o arquivo.
5. **O que é irreversível e por isso vem por último:** descarte de backups com senha de cliente (R6). Antes dele: sanear a origem, rotacionar credenciais e abrir a avaliação de incidente LGPD com o jurídico.
6. **A ordem recomendada é R0 → R1 → R2 → R3 → R4 → R5 → R6.** As três primeiras são marcação; R4 mexe em rede; R5 mexe em disco; R6 destrói cópias.

| Item | Objeto | Efeito | Reversível? | Rede? | Bloqueia outro item? |
|---|---|---|---|---|---|
| R0 | Baseline e medição | nenhum (leitura) | — | medir_fontes usa | todos |
| R1 | ~900 lotes vazios | `ignorado=True` | sim (desmarcar) | não | R3, R4 |
| R2 | lotes liberados por CPF-por-nome | volta a `REVISAO` | sim | não | R3 |
| R3 | 503 descartes por duplicata | suspeitos → `revisao_duplicata` | sim | não | R4 |
| R4 | 453 não baixados / 83 erros | rebaixa e re-baixa | sim (idempotente) | **sim** | R3 final, R5 |
| R5 | `.parcial`/`.part`/0 byte/caminho compartilhado | quarentena e rebaixa | sim (quarentena) | sim (rebaixar) | — |
| R6 | backups, fila e `/tmp` com senha | retenção + descarte | **não** (destruição) | não | — |

---

## 2. Princípios e travas

### 2.1 As três fases, em toda ação

| Fase | O que é permitido | Saída obrigatória |
|---|---|---|
| **Somente leitura** | `inventario_estado.py`, `medir_fontes.py`, `grep`, `sha256sum`, `du`, `find … -printf` | contagens e ids; **nunca** nome, CPF, telefone ou texto de mensagem |
| **Simulação** | o mesmo comando/patch com `--simular` ou `simular=True`; grava a lista de alvos em arquivo | lista de ids + contagem + `sha256` do estado no momento |
| **Aplicação autorizada** | patch via `estado_store.atualizar_lotes` (dono correto) ou `podar_backups(simular=False)` | difusão antes/depois, `sha256` novo, linha no livro de execução |

**Regra de ouro:** não existe aplicação sem simulação do **mesmo** comando, sobre o **mesmo** estado, na **mesma** janela. Se o `sha256` do `estado.json` mudou entre simular e aplicar, a simulação é descartada e refeita.

### 2.2 Pré-requisitos técnicos (verificar, não presumir)

- [ ] Onda 1 implantada: `estado_store`, `estado_lote`, `bitrix_cliente`, `rede_bitrix`, `seguranca`, `tipos_documento` em produção e o ciclo usando merge por campo (sem isso, R1/R3/R2 são apagados — A3).
- [ ] `estado_store.ler_estado` devolve `EstadoCorrompido` em vez de `{}` e o ciclo **aborta** em vez de "começar do zero" (A2). Se ainda zerar, não aplicar nada.
- [ ] `estado_lote.estado_logico`/`pode_enfileirar` rodando contra o estado real (é o contador oficial do plano).
- [ ] `manutencao/inventario_estado.py` e `manutencao/medir_fontes.py` entregues (TAREFAS 1 e 2). **As flags citadas abaixo vêm do brief; conferir `--help` na entrega antes de rodar.**
- [ ] Janela de manutenção acordada e timers parados no momento da aplicação (`triagem-lote-fila`, `triagem-bitrix-ponte`, `triagem-bitrix-certidoes`, `pintar-linha-triagem`). Sem timers parados, R1/R3/R2 competem com a ponte e com o ciclo.
- [ ] Cópia de segurança verificada do estado **antes** de cada aplicação (`estado_store.fazer_backup`, `0600`, e conferência de `sha256`), em diretório `0700`.
- [ ] Livro de execução aberto (modelo no §12) e autorização registrada (§11).

### 2.3 Regras que valem para todos os itens

- **Não apagar lote.** Nunca. O `telegram_grupo.reforcar_lotes_salvos` recria lote removido a partir de `lotes_memoria` (TG-02); a limpeza é por **marcação**.
- **Não reprocessar automaticamente** nada que dependa de identidade (CPF, nome, vínculo com negócio). R2 é o caso-tipo: volta para revisão humana, sempre.
- **Não imprimir PII.** Saída padrão do plano tem `lote_id`, contagem e estado lógico. Nome/CPF só em tela do painel, para quem já tem acesso a ele.
- **Não burlar o disjuntor.** R4 espera o `circuit-*` fechar com sonda real; não se apaga `circuit-download.json` para "destravar" (B3).
- **Escrita pelo dono do campo.** `estado_store` recusa campo de outro dono: `status` é de `triagem` (a `ponte` só escreve `descartado_duplicata`/`revisao_duplicata`), `arquivos[*].baixado|sha256|caminho` é de `download`, `requer_revisao|motivos_revisao|pronto` é de `revisao`, `cpf|identificacao` é de `identificacao`. Um patch com dono errado é **ignorado em silêncio** — conferir a lista de conflitos devolvida por `atualizar_lotes` (vazia = aplicou).
- **Duas passadas quando o item toca dois donos** (é o caso de R3): primeiro `status` como `ponte`, depois `motivos_revisao` como `revisao`.

### 2.4 Verificação deste plano (o que já foi rodado)

- Os **12 blocos Python** deste documento compilam em **Python 3.9.6** (`.venv/bin/python -m py_compile`).
- As receitas de **R1, R2 e R3** foram executadas contra uma fixture sintética de 11 lotes (CPF de `cpf_util.gerar_para_teste`, nomes fictícios), com `estado_store.atualizar_lotes` real: R1 marcou 3 de 3 vazios e **não** tocou o lote vazio que tinha nome; R2 devolveu o lote `cpf_origem="bitrix-nome"` à revisão preservando `cpf`; R3 converteu só o descarte com download incompleto e deixou o duplicata completa em paz. Todas as passadas devolveram `conflitos {}`.
- Achado de implementação incorporado: **`cpf_sugerido` não é gravável por script de manutenção** quando existe `cpf_origem` (o campo é reivindicado por `identificacao`); por isso o passo 2 de R2 grava apenas `cpf_confiavel`.
- `manutencao/inventario_estado.py` foi executado nos dois modos (texto e `--json`) contra a fixture: as chaves usadas nas receitas (`arquivos_parciais`, `backups_estado`, `lotes_com_senha_no_texto`) existem.
- Suíte do repositório no momento da entrega: **745 passam, 14 falham**; as 14 são as conhecidas que leem `/opt/triagem-bitrix/estado.json` (ausente fora do servidor) — nenhuma delas toca este documento.

---

## 3. R0 — Baseline e medição (somente leitura)

**Objetivo:** substituir os números de 09/11 pelos de hoje e provar que a leitura é segura (sem PII).

```bash
BASE=/opt/triagem-bitrix
PY=$BASE/venv/bin/python
mkdir -p /root/recuperacao && chmod 700 /root/recuperacao
cd "$BASE"

# 0.1 estado do serviço e hash do estado
systemctl list-timers --all | grep triagem | tee /root/recuperacao/timers-$(date +%F).txt
sha256sum estado.json | tee /root/recuperacao/hash-$(date +%F-%H%M).txt
du -sh estado.json lotes backups; df -h "$BASE"

# 0.2 inventário agregado (contagens apenas)
$PY manutencao/inventario_estado.py --json > /root/recuperacao/baseline-$(date +%F).json
$PY manutencao/inventario_estado.py            # versão texto, para o relatório

# 0.3 fontes de leitura (rede; só com autorização — §11)
$PY manutencao/medir_fontes.py --chat chat100001 --chat chat100002 --ultimas 200 --max-req 60
```

**Gate de entrada:** não avançar para R1/R2/R3 se (a) o inventário não fechar com `git`/`du` (ex.: `estado.json` cresceu ou encolheu > 5% desde a medição anterior sem explicação), (b) o estado estiver corrompido (`EstadoCorrompido`), ou (c) houver conflito de escrita recente no log. Nesses casos, primeiro a integridade (onda 1), depois a recuperação.

**O que o baseline precisa responder** (todas são contagens, nenhuma é PII):

- total de lotes; por `status`, `identificacao` e `estado_lote.estado_logico`; `pronto × requer_revisao`;
- `motivos_revisao` top N; lotes sem arquivo; lotes sem `file_ids`;
- arquivos por tipo (`tipos_documento.classificar`); não baixados por fonte e por erro normalizado;
- ids `CQ…` (Telegram legado); `cpf_origem`; CPFs com DV inválido (contagem);
- lotes cujo texto casa marcador de senha (contagem); quantos `pode_enfileirar` aprovaria hoje e por que os demais não passam;
- `.parcial`/`.part`/0 byte no diretório de lotes; backups `estado.json.bak-*` (quantidade e bytes).

**Observação de método (dado inconsistente na origem):** o CONTEXTO registra 900 lotes com zero arquivo e 615 com `identificacao="SEM_ARQUIVOS"`. Como 900 > 615, o rótulo `identificacao` não é uniforme no estado legado. **A classificação de R1 usa `arquivos[]`/`file_ids`, não o rótulo** — o inventário precisa reportar as duas contagens lado a lado.

---

## 4. R1 — ~900 lotes vazios → `IGNORADO`

**Situação (histórico):** ~900 lotes sem nenhum arquivo; 850 deles com 1–2 mensagens, resíduo do corte da janela de leitura (o cursor por chat é gravado e nunca lido; quando a janela anda, o começo vira "lote novo"). Inflam o painel, a contagem de SEM_ARQUIVOS e o ruído do monitor (F2-07).

**O que se recupera:** nada de documento — recupera-se **sinal**. O lote vazio deixa de poluir contadores e monitor; o kit legítimo eventualmente escondido atrás de um lote vazio continua intocado.

### Critério de seleção (todos obrigatórios)

```text
1. arquivos == []  e  file_ids == []
2. nenhuma mensagem do lote tem file_ids (mensagens[].file_ids vazio/ausente)
3. sem cpf, sem cpf_sugerido e sem nome não vazio
4. não é sub-lote: sem dividido_em, sub_lotes, sublotes; status != DIVIDIDO
5. estado_logico atual != IGNORADO/DESCARTADO_DUPLICATA/DIVIDIDO
6. lote_id não aparece em fila-enviados.json (nem como job_id, nem em upload_id)
```

Qualquer lote que falhe em 1–6 **não é ignorado**: se tem nome/CPF mas nenhum arquivo, vai para revisão com o motivo `lote sem arquivos com identificação` (é caso de kit perdido, não de resíduo). Essa é a diferença entre limpar ruído e esconder perda.

### Simulação

```bash
$PY - <<'PY' | tee /root/recuperacao/r1-candidatos.txt
from __future__ import annotations
import json, caminhos, estado_store, estado_lote

est = estado_store.ler_estado()
try:
    with open(caminhos.em_base("fila-enviados.json"), encoding="utf-8") as fh:
        ctrl = json.load(fh)
except OSError:
    ctrl = {}
enviados = set(ctrl) | {str((v or {}).get("job_id")) for v in ctrl.values() if isinstance(v, dict)}
enviados |= {str((v or {}).get("upload_id")) for v in ctrl.values() if isinstance(v, dict)}

vazios, com_identidade, ja = [], [], 0
for lid, lote in (est.get("lotes") or {}).items():
    if estado_lote.estado_logico(lote, ctrl.get(lid)) in ("IGNORADO", "DESCARTADO_DUPLICATA", "DIVIDIDO"):
        ja += 1; continue
    if (lote.get("arquivos") or []) or (lote.get("file_ids") or []):
        continue
    if any((m or {}).get("file_ids") for m in (lote.get("mensagens") or [])):
        continue
    if lote.get("dividido_em") or lote.get("sub_lotes") or lote.get("sublotes"):
        continue
    if lid in enviados:
        continue
    if (lote.get("cpf") or lote.get("cpf_sugerido") or str(lote.get("nome") or "").strip()):
        com_identidade.append(lid); continue
    vazios.append(lid)

print(f"total={len(est.get('lotes') or {})} ja_marcados={ja} candidatos={len(vazios)} "
      f"com_identidade_sem_arquivo={len(com_identidade)}")
for lid in vazios:
    print(lid)
PY
```

**Conferência antes de aplicar:** `candidatos` deve ser da mesma ordem de grandeza do baseline; `com_identidade_sem_arquivo` deve ser pequeno e é o que vai para revisão. Se `candidatos` vier muito acima do esperado, há lote legítimo com `arquivos` vazio por bug de download — **parar** e ir para R4 antes.

### Aplicação (autorizada, timers parados)

```python
# dono "triagem": `ignorado` não tem dono declarado e cai em triagem; `status` é de triagem.
from __future__ import annotations
import datetime, estado_store

MOTIVO = "residuo de janela de leitura (R1)"
ALVOS = [ln.strip() for ln in open("/root/recuperacao/r1-candidatos.txt", encoding="utf-8")
         if ln.strip() and "=" not in ln]
mudancas = {lid: {"ignorado": True,
                  "ignorado_em": datetime.datetime.now().isoformat(timespec="seconds"),
                  "ignorado_motivo": MOTIVO,
                  "ignorado_por": "recuperacao-r1"}
            for lid in ALVOS}                      # ALVOS = lista lida de r1-candidatos.txt
conflitos = estado_store.atualizar_lotes(mudancas, dono="triagem")
print("conflitos:", conflitos)                     # tem de ser {}
```

Usar a **flag `ignorado`**, não sobrescrever `status`: `estado_lote._ignorado` lê a flag primeiro, o valor legado de `status` fica preservado para auditoria e a marcação sobrevive ao ciclo. `status="IGNORADO"` também é reconhecido, mas escreve por cima de valor que a triagem usa.

### Critério de parada

- `conflitos` não vazio → parar e revisar dono de campo.
- qualquer lote da lista com `arquivos`/`file_ids` não vazio no momento da aplicação (estado mudou) → refazer a simulação.
- `EstadoCorrompido`, erro de `flock`/timeout → parar; não repetir em loop.

### Rollback

Reversível por construção: `atualizar_lotes({lid: {"ignorado": False}}, dono="triagem")` (a flag cai; `ignorado_em/motivo` ficam como histórico). Não há arquivo apagado.

### Efeito no painel

Lotes vazios deixam de aparecer como pendência; `SEM_ARQUIVOS` cai sem que nenhum kit com documento seja tocado. Monitor (F2-07) para de reavisar o mesmo conjunto.

### Quem autoriza

Responsável técnico designado, com a lista de simulação aprovada. **Não** exige o sócio: não há destruição nem rede.

---

## 5. R2 — Lotes liberados por "CPF por nome" sem 2ª evidência (ID-01/ID-02)

**Situação (histórico):** `completar_cpf_por_nome.py` buscava por LIKE no **primeiro nome**, lia só a 1ª página (50 contatos), aceitava "exatamente 1 casamento" nessa página e, ao achar, escrevia `identificacao="OK"`, `requer_revisao=False`, `motivos_revisao=[]` e `pronto=True` (ID-01/ID-02). Resultado possível: lote com CPF de homônimo no painel, contrato no nome errado, e motivo de revisão legítimo apagado (lote misto, duplicata em revisão, fechamento por janela).

**O que se recupera:** a **revisão humana** desses lotes. Não se "corrige" o CPF — não há como saber, sem 2ª âncora, se o CPF é do titular. O plano devolve o lote para revisão e **preserva o campo `cpf` como candidato**, marcado com `cpf_confiavel=False` para a pintura e a ponte não o tratarem como âncora.

### Critério de seleção

```text
1. cpf_origem == "bitrix-nome"            (marcador gravado por completar_cpf_por_nome.py:139)
2. e NÃO existe 2ª âncora registrada: sem telefone do contato, sem autor, sem OCR,
   sem `cpf_origem` de fonte forte (ficha/ocr)
3. independente de estar `pronto` hoje
```

A âncora é uma decisão de dado, não de código: o inventário conta quantos lotes têm `cpf_origem="bitrix-nome"` e, entre eles, quantos têm algum campo de telefone/autor preenchido. **Lista completa só no painel** (com CPF), nunca em log.

### Simulação

```bash
$PY - <<'PY' | tee /root/recuperacao/r2-candidatos.txt
from __future__ import annotations
import estado_store, estado_lote
est = estado_store.ler_estado()
alvos = [lid for lid, l in (est.get("lotes") or {}).items()
         if str(l.get("cpf_origem") or "") == "bitrix-nome"]
print(f"cpf_origem=bitrix-nome: {len(alvos)}")
for lid in alvos:
    print(lid)
PY
```

Conferir com o inventário (`--json`, contagem por `cpf_origem`). Divergência > 0 entre os dois → parar e reconciliar leitura.

### Aplicação (autorizada, timers parados)

```python
# Passo 1 (dono "revisao"): devolve o lote à revisão; `marcar_revisao` acrescenta motivo e põe pronto=False.
from __future__ import annotations
import estado_store, estado_lote

MOTIVO = "cpf por nome sem 2a evidencia (ID-01/ID-02)"
ALVOS = [ln.strip() for ln in open("/root/recuperacao/r2-candidatos.txt", encoding="utf-8")
         if ln.strip() and "=" not in ln]
estado = estado_store.ler_estado()
mudancas = {}
for lid in ALVOS:
    lote = (estado.get("lotes") or {}).get(lid) or {}
    if not lote or str(lote.get("cpf_origem") or "") != "bitrix-nome":
        continue
    novo = estado_lote.marcar_revisao(lote, MOTIVO)
    mudancas[lid] = {"requer_revisao": True,
                     "motivos_revisao": novo["motivos_revisao"],
                     "pronto": False}
print("conflitos revisao:", estado_store.atualizar_lotes(mudancas, dono="revisao"))
```

```python
# Passo 2 (dono "triagem": `cpf_confiavel` ainda não tem dono declarado em PROPRIEDADE_CAMPOS).
# NÃO escrever `cpf_sugerido`: com `cpf_origem` preenchido, o campo é reivindicado por
# "identificacao" e a escrita por script de manutenção é recusada — o CPF candidato já está em
# `cpf`, que este plano preserva.
from __future__ import annotations
import estado_store

ALVOS = [ln.strip() for ln in open("/root/recuperacao/r2-candidatos.txt", encoding="utf-8")
         if ln.strip() and "=" not in ln]
lotes = estado_store.ler_estado().get("lotes") or {}
mudancas = {lid: {"cpf_confiavel": False} for lid in ALVOS if lid in lotes}
print("conflitos sugestao:", estado_store.atualizar_lotes(mudancas, dono="triagem"))
```

`cpf` e `identificacao` **não** são reescritos: continuam como estão, para o painel mostrar o candidato. `cpf_confiavel=False` é o sinal novo que a pintura e a ponte devem consultar (pendência para a frente que cuida de `pintar_linha`/`lote_para_fila`).

### Critério de parada

- conflito de dono em `requer_revisao`/`pronto` → parar (outra frente está escrevendo).
- lote que já não tem `cpf_origem="bitrix-nome"` → ignorar, não "corrigir".
- qualquer tentativa de rodar `completar_cpf_por_nome` de novo durante a recuperação → **proibido** até a correção com paginação + 2ª âncora estar implantada; rodar de novo recria o problema.

### Rollback

Reverter exige decisão humana (o lote só volta a `pronto` por aprovação explícita, com `resolver_revisao`). Não há caminho automático de volta — de propósito.

### Quem autoriza

Responsável técnico + responsável pela operação do painel. Se a lista incluir lote já enviado ao painel, a decisão de reverter contrato/pasta é do **sócio responsável** (não é recuperação de dado, é correção de produção).

---

## 6. R3 — 503 descartes por duplicata: separar falso descarte (F2-03)

**Situação (histórico):** 503 lotes com `status="descartado_duplicata"`. O dedup é por **subconjunto**: se todos os `sha256[:12]` **baixados** já foram enviados para o mesmo CPF, o lote é descartado — mesmo que existam arquivos ainda **não baixados** que seriam novos (F2-03). Cliente que reenvia o RG e manda um comprovante novo durante o bloqueio de download pode ter o kit descartado como duplicata.

**O que se recupera:** a possibilidade de envio legítimo. O plano não reenvia nada: transforma o falso descarte em `revisao_duplicata` (decisão humana), que é o estado que o código já reconhece.

### Critério de seleção

```text
1. status == "descartado_duplicata"
2. existe arquivo no lote com baixado != True  OU  sem sha256  (download incompleto)
3. e esses arquivos têm file_id conhecido (dá para baixar depois)
```

Lote descartado com **todos** os arquivos baixados e com hash ⊆ histórico do CPF é duplicata genuína: **não entra**. Lote descartado com hash **novo** entre os baixados não deveria ter sido descartado — também entra, e o relatório separa os dois casos (`motivo: subconjunto` × `motivo: hash novo`).

### Simulação

```bash
$PY - <<'PY' | tee /root/recuperacao/r3-candidatos.txt
from __future__ import annotations
import estado_store
est = estado_store.ler_estado()
sub, novo = [], []
for lid, l in (est.get("lotes") or {}).items():
    if str(l.get("status") or "") != "descartado_duplicata":
        continue
    arqs = [a for a in (l.get("arquivos") or []) if isinstance(a, dict)]
    pend = [a for a in arqs if a.get("baixado") is not True or not a.get("sha256")]
    if not pend:
        continue
    baixados = [a for a in arqs if a.get("baixado") is True and a.get("sha256")]
    (sub if baixados else novo).append(lid)   # subconjunto parcial x nada baixado
print(f"descartados_com_download_incompleto={len(sub) + len(novo)} "
      f"subconjunto_parcial={len(sub)} sem_hash_nenhum={len(novo)}")
for lid in sub + novo:
    print(lid)
PY
```

**Duas passadas, dois donos** (o estado recusa `status` para quem não é `ponte`):

```python
# Passo 1 — dono "ponte": só valores de STATUS_PONTE são aceitos (revisao_duplicata é um deles).
from __future__ import annotations
import estado_store
ALVOS = [ln.strip() for ln in open("/root/recuperacao/r3-candidatos.txt", encoding="utf-8")
         if ln.strip() and "=" not in ln]
mudancas = {lid: {"status": "revisao_duplicata",
                  "descarte_motivo": "subconjunto com download incompleto (F2-03)"}
            for lid in ALVOS}
print("conflitos status:", estado_store.atualizar_lotes(mudancas, dono="ponte"))
```

```python
# Passo 2 — dono "revisao": motivo visível para quem decide no painel.
from __future__ import annotations
import estado_store, estado_lote
MOTIVO = "duplicata por subconjunto — kit incompleto (F2-03)"
ALVOS = [ln.strip() for ln in open("/root/recuperacao/r3-candidatos.txt", encoding="utf-8")
         if ln.strip() and "=" not in ln]
lotes = estado_store.ler_estado().get("lotes") or {}
mudancas = {}
for lid in ALVOS:
    lote = lotes.get(lid) or {}
    if not lote or lote.get("status") != "revisao_duplicata":
        continue
    novo = estado_lote.marcar_revisao(lote, MOTIVO)
    mudancas[lid] = {"requer_revisao": True, "motivos_revisao": novo["motivos_revisao"],
                     "pronto": False}
print("conflitos revisao:", estado_store.atualizar_lotes(mudancas, dono="revisao"))
```

**Não** restaurar `status` para o valor de `status_anterior` por conta própria: o ciclo e a ponte tratam `revisao_duplicata` como "decisão humana"; voltar para `FECHADO_*` faria a ponte reenviar sem ninguém olhar.

### Segunda avaliação (obrigatória, depois de R4)

Com o download completo, refazer o dedup **somente leitura** (`lote_para_fila.py --simular --lote <id> --dias 0`) para cada candidato:

- kit completo e todos os hashes ⊆ histórico → é duplicata real: volta para `descartado_duplicata` (dono `ponte`) com `descarte_motivo="confirmado após download completo"`;
- existe hash novo → segue para envio normal (a ponte faz isso; o plano só libera a marcação com `resolver_revisao`).

### Critério de parada

- `--simular` da ponte retornar lote fora da janela de 7 dias sem `--dias 0` → ajustar, não forçar.
- qualquer lote cujo CPF no estado não passe em `cpf_util.valido` → não avaliar dedup; mandar para R2.
- taxa de candidatos acima de ~10% dos 503 → parar: provavelmente o download ainda está bloqueado e **todo** descarte recente parece suspeito. Resolver R4 primeiro.

### Rollback

`status="descartado_duplicata"` de volta (dono `ponte`), `descarte_motivo` restaurado, motivo removido via `resolver_revisao`. O `status_anterior` nunca é apagado.

### Quem autoriza

Responsável técnico para a marcação; operação do painel para decidir caso a caso. Nenhum envio é feito por este plano.

---

## 7. R4 — 453 não baixados e 83 com erro real: retry dirigido

**Situação (histórico):** 453 arquivos sem download (370 "só adiados", 78 pausados pelo disjuntor, 5 falhas reais) e 83 arquivos em 38 lotes com erro real, retentados 3×/10 min para sempre (D1). O disjuntor de download abriu em 09/11 13:06 e o arquivo nunca é fechado nem limpo; `baixar_pendentes`/`download_svc` ignoram o disjuntor e martelam o portal (B3).

**O que se recupera:** os arquivos que ainda existem no portal e cujo erro é transitório. O que for 404/403 permanente, id não numérico ou > 20 MB do bot **não se recupera por retry** — vira `erro_permanente` e sai do loop.

### Ordem obrigatória

1. **Fechar o disjuntor com sonda real**, não apagando o JSON: uma chamada de leitura bem-sucedida via `bitrix_cliente.chamar(..., circuito="bitrix-download")`. Se o portal segue bloqueando, **R4 não começa** — o plano espera.
2. **Reclassificar antes de baixar.** Classificar cada arquivo pendente por `rede_bitrix.classificar` (401/403/404/`ACCESS_DENIED`/`NOT_FOUND`/id não numérico/> 20 MB do bot = permanente; 429/503/timeout/reset = transitório). Permanente não é retentado: `baixado=False`, `erro_tipo="permanente"`, `erro` normalizado.
3. **Retry em ondas**, com orçamento: começar pelos 38 lotes com erro real, 1 arquivo por vez, teto de requisições por execução.

```bash
BASE=/opt/triagem-bitrix; PY=$BASE/venv/bin/python
cd "$BASE"

# 7.1 mapa dos pendentes (leitura; sem PII)
$PY manutencao/inventario_estado.py --download-fila "$BASE/download_fila.json"   # resumo por fonte/erro
$PY - <<'PY'
from __future__ import annotations
import collections, estado_store
est = estado_store.ler_estado()
por_erro = collections.Counter()
for lid, l in (est.get("lotes") or {}).items():
    for a in (l.get("arquivos") or []):
        if a.get("baixado") is True:
            continue
        por_erro[str(a.get("erro_tipo") or a.get("erro") or "sem_erro")[:60]] += 1
print("pendentes:", sum(por_erro.values()))
for k, v in por_erro.most_common(20):
    print(f"{v:5d}  {k}")
PY

# 7.2 retry dirigido, um lote por execução, com teto (ver receita abaixo)
$PY /root/recuperacao/retry_lote.py --lote LOTE-... --max-req 20 --simular
$PY /root/recuperacao/retry_lote.py --lote LOTE-... --max-req 20 --aplicar
```

**Receita do retry** (script operacional de uma via, guardado em `/root/recuperacao/`, **fora do deploy** — não é arquivo do repositório; a versão de produção do retry é a correção de `baixar_pendentes`/`download_svc`, de outra frente):

```python
from __future__ import annotations
import argparse, hashlib, os, time, urllib.request
import bitrix_cliente, estado_store, rede_bitrix, tipos_documento, cpf_util  # noqa: F401

def baixar_um(fid, destino_tmp, limite):
    limite.aguardar()
    dados = bitrix_cliente.chamar("disk.file.get", {"id": int(fid)}, circuito="bitrix-download")
    url = (dados.get("result") or {}).get("DOWNLOAD_URL")
    if not url:
        raise RuntimeError("sem DOWNLOAD_URL")
    with urllib.request.urlopen(url, timeout=30) as r, open(destino_tmp, "wb") as fh:
        while True:
            bloco = r.read(256 * 1024)
            if not bloco:
                break
            fh.write(bloco)
        fh.flush(); os.fsync(fh.fileno())
    sha = hashlib.sha256(open(destino_tmp, "rb").read()).hexdigest()
    if os.path.getsize(destino_tmp) <= 0:
        raise RuntimeError("arquivo vazio")
    return sha
```

Contrato do script: lê o lote, pula `erro_tipo=="permanente"`, escreve em `.part` no diretório do lote, confere tamanho e sha, faz `os.replace`, e grava o resultado com `atualizar_lotes(..., dono="download")` (`arquivos[*].baixado/sha256/caminho/tentativas/proximo_retry`). Em `FalhaBitrix`/`CircuitOpenError`: **parar a rodada** e registrar; não tentar o próximo arquivo do mesmo lote.

### Critério de parada

- `RATE_LIMIT`/`OPERATING_LIMIT`/disjuntor abrindo → parar imediatamente, não insistir (é o que causou o estado atual).
- orçamento (`--max-req`) estourado → fim da execução, retomar no próximo ciclo autorizado.
- erro permanente → marcar e seguir; não conta como falha da rodada.
- mais de 20% de falha transitória numa onda → parar e reavaliar a saúde do portal com `medir_fontes.py`.

### Rollback

Idempotente: o arquivo é gravado em `.part` e só promovido com tamanho/sha conferidos; estado antigo preservado em backup. Reverter = restaurar o backup e remover `.part`.

### Quem autoriza

**Sócio/dono do escritório** (ou responsável técnico com autorização escrita), porque é a única fase que fala com o portal em volume e consome cota compartilhada com a operação. Orçamento e janela combinados antes.

---

## 8. R5 — Integridade em disco: `.parcial`, 0 byte, caminho compartilhado (E1/E3)

**Situação (histórico):** `.parcial` órfão faz `baixar_pendentes` pular o índice **para sempre** (E1); `.part`/0 byte são aceitos por reconciliadores (E4); dois anexos com o mesmo nome no lote caem no mesmo caminho e o segundo é dado como "reaproveitado" — documento some sem erro (E3).

### 8.1 Inventário (leitura)

```bash
BASE=/opt/triagem-bitrix
find "$BASE/lotes" -type f \( -name '*.parcial' -o -name '*.part' -o -name '*.tmp' -o -size 0 \) \
  -printf '%p\t%s\n' | tee /root/recuperacao/r5-sujos.txt
wc -l /root/recuperacao/r5-sujos.txt
$PY manutencao/inventario_estado.py --lotes-dir "$BASE/lotes" --json \
  | python3 -c 'import json,sys; print("parciais/0 byte:", json.load(sys.stdin)["arquivos_parciais"])'
```

Colisão de nome (E3) — detectar no estado, sem PII:

```python
from __future__ import annotations
import collections, estado_store
est = estado_store.ler_estado()
for lid, l in (est.get("lotes") or {}).items():
    chaves = collections.Counter((a.get("caminho"), a.get("sha256"))
                                 for a in (l.get("arquivos") or []) if a.get("baixado") is True)
    dup = [k for k, n in chaves.items() if n > 1 and k[0]]
    if dup:
        print(lid, len(dup), "caminho(s) compartilhado(s)")
```

### 8.2 Aplicação

1. **Quarentena, não `rm`:** mover `.parcial`/`.part`/0 byte para `$BASE/quarentena/<data>/` com `0700` e manifesto (`nome`, tamanho, sha256, lote). Só o dono do escritório autoriza apagar depois de 7 dias.
2. **Rebaixar o estado para rebaixar de novo:** para cada arquivo afetado, `atualizar_lotes(..., dono="download")` com `baixado=False`, `sha256=None`, `caminho=None`, `tentativas=0`, `proximo_retry` no próximo ciclo. Sem isso, o estado continua dizendo "baixado" e a ponte envia lote corrompido (F2-02).
3. **Colisão (E3):** o arquivo perdido **não volta do disco**. Marcar o lote com `marcar_revisao("documento duplicado por nome — E3")` e, na revisão, decidir entre (a) re-baixar o `file_id` que ficou sem arquivo próprio (se ainda existir no portal — é o caminho preferido) ou (b) pedir o documento ao cliente. Nunca "consertar" copiando o mesmo arquivo para os dois metas: isso fabrica prova.
4. **Ordem:** R5 vem **depois** de R4 — rebaixar tudo antes de o portal voltar só gera fila de retry concorrente.

### Critério de parada

- mais de ~50 arquivos sujos → investigar causa antes de mover (provável crash recente em série; conferir `journalctl` e espaço em disco).
- qualquer caminho fora de `$BASE/lotes/` → parar (é sinal de path traversal ou lote antigo fora do padrão).
- `estado.json` divergindo do disco em > 1% após o rebaixamento → reconciliar com `reconciliar_arquivos_disco.py` antes de seguir.

### Rollback

`mv` de volta da quarentena e restauração dos campos a partir do backup do estado, na mesma janela. A quarentena existe justamente para isso.

### Quem autoriza

Mover para quarentena: responsável técnico. Apagar a quarentena: **sócio** (dado de cliente). Re-baixar arquivo: mesma autorização de R4.

---

## 9. R6 — Dados sensíveis: backups, fila e `/tmp` com senha de cliente (S01/S05)

**Situação (histórico):** a senha gov.br/Meu INSS do cliente é extraída e replicada em texto puro: `estado.json` (texto bruto das mensagens), **1.833 `estado.json.bak-*` (27 GB)**, `fila-jobs.json`, `backups/fila-jobs-*.json` (737 MB), `/tmp/_entradas.json`. Não existe retenção em nada disso (S01/S05). O `ponte.log` (208 MB) acumula nome + CPF (S04).

**Regra dura:** este item **destrói cópias**. Nada aqui é reversível. Por isso a ordem interna é: medir → sanear a origem → rotacionar → avaliar LGPD → só então descartar.

### 9.1 Medir (leitura)

```bash
BASE=/opt/triagem-bitrix
ls -1 "$BASE"/estado.json.bak-* 2>/dev/null | wc -l
du -sh "$BASE"/estado.json.bak-* 2>/dev/null | tail -1
find "$BASE/backups-estado" "$BASE/backups" -maxdepth 1 -type f -name '*.json*' 2>/dev/null | wc -l
du -sh "$BASE/backups" "$BASE/ponte.log" 2>/dev/null
ls -l /tmp/_entradas.json /tmp/certidao_* /tmp/b24_webhook.txt /tmp/_enfileirar.js 2>/dev/null
$PY manutencao/inventario_estado.py --json \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print("backups:", d["backups_estado"], "| lotes com senha:", d["lotes_com_senha_no_texto"])'
```

A contagem de **lotes cujo texto casa marcador de senha** é o único número que dimensiona o incidente. Ele sai do inventário (contagem), nunca do texto.

### 9.2 Sanear a origem (antes de apagar qualquer cópia)

Sem isso, o ciclo seguinte regrava a senha e o descarte é teatro:

1. Implantar `seguranca.remover_senhas` **no caminho de persistência** (antes de `serializar_lote` e antes de montar a fichinha). É mudança de comportamento em `triagem_grupo.py`/`lote_para_job.py` — **pertence a outra frente**; este plano só registra a dependência e não a executa.
2. Com a origem corrigida e os timers parados, sanear o `estado.json` corrente uma vez: mutador que aplica `seguranca.remover_senhas` em `mensagens[].texto`, via `atualizar_estado`, com backup verificado antes e contagem antes/depois de lotes com marcador.
3. Sanear `fila-jobs.json` só se o painel não precisar do campo; se precisar, a decisão é cifrar (chave fora do disco de dados) — decisão do sócio + jurídico, não do plano.

### 9.3 Rotacionar credenciais (paralelo, independente do descarte)

- webhook Bitrix (`rest/1/…`) → webhook de usuário técnico com escopo mínimo (S06);
- credencial de cliente hardcoded em `lote_para_fila.py:435` → tratar como comprometida e avisar a cliente para trocar (S02);
- chave VibeCode → uma fonte única `0600` (S06);
- remover `/tmp/b24_webhook.txt` **depois** da rotação, não antes.

### 9.4 Retenção e descarte

```python
# 1) simular (padrão NUNCA apaga) — revisar a lista antes
from __future__ import annotations
import estado_store
apagar = estado_store.podar_backups(manter_ultimos=24, diarios=14, simular=True)
print(len(apagar), "candidatos"); print("\n".join(apagar[:10]))
```

- **Backups do estado:** `podar_backups(manter_ultimos=24, diarios=14, simular=True)` → aprovar lista → mover para `$BASE/quarentena/backups-<data>/` (mesmo FS, `mv`, sem custo de cópia) → após 7 dias e com autorização, apagar. `podar_backups(simular=False)` usa `unlink`; **não é apagamento seguro de verdade** em SSD/journaling — o controle real é criptografia de disco e, se houver cópia fora do servidor (WSL, backup do Windows, sincronização), tratá-la como vazamento. Não afirmar "apagado com segurança" sem essa ressalva.
- **`backups/fila-jobs-*`:** mesma política, por data; são pequenos, mas contêm fichinha + CPF + senha.
- **`/tmp`:** `_entradas.json`, `_enfileirar.js`, `certidao_*.pdf` (certidão de menor), `b24_webhook.txt`. Ordem: rotacionar segredo → apagar arquivo. `certidao_*` só depois de confirmar que a tarefa no CRM tem o anexo (o arquivo em `/tmp` é cópia, não original).
- **`ponte.log`/journal (S04):** `logrotate` (diário, `rotate 14`, `compress`, `create 0600`) e `MaxRetentionSec` no journald; a limpeza do histórico de log é decisão separada (contém nome+CPF).

### 9.5 Avaliação de incidente LGPD

O relatório 01 classifica o caso como "incidente de segurança reportável à ANPD" e "responsabilidade civil" — é a **leitura da auditoria**, não uma conclusão jurídica deste plano. O que este plano faz é produzir o registro para o jurídico decidir:

| Campo a registrar | Como obter |
|---|---|
| data/hora da descoberta e da contenção | log do livro de execução |
| categorias de dado | nome, CPF, telefone, e-mail, senha gov.br, documentos (RG/CNH/comprovante), certidão de menor |
| titulares estimados | contagem de lotes com senha + contatos distintos no estado (contagem, sem listar) |
| cópias afetadas | estado, nº de backups, `fila-jobs*`, `/tmp`, logs, eventuais cópias fora do servidor |
| medidas tomadas | saneamento da origem, retenção, descarte, rotação, restrição de permissão (`UMask`, `User=`) |
| risco avaliado | probabilidade × gravidade, com a senha gov.br como fator de acesso a INSS/CNIS |
| decisão | comunicar titulares / ANPD / nenhuma, com justificativa e data |

**Quem decide:** sócio responsável + jurídico/encarregado de dados do escritório. **Prazos da ANPD:** pontos a **confirmar com o jurídico** na data do fato — este documento não afirma prazo, base legal ou obrigação de comunicar; a fonte (auditoria) também não fixa prazo.

### Critério de parada

- origem ainda gravando senha → **não descartar nada** (perde-se a única evidência e o problema volta).
- backup mais novo corrompido ou não verificável → não podar; primeiro garantir um backup íntegro.
- jurídico ainda não decidiu sobre preservação de evidência → parar no estágio de quarentena.

### Rollback

Não existe para o descarte. Existe para a **quarentena** (mover de volta dentro dos 7 dias). É a razão de a quarentena existir.

### Quem autoriza

**Sócio responsável + jurídico** (retenção e descarte de dado pessoal). Rotação de credencial: sócio/TI. Nenhum descarte por iniciativa técnica.

---

## 10. Ordem, dependências e esforço

| # | Etapa | Depende de | Estimativa* | Paralelizável com |
|---|---|---|---|---|
| R0 | baseline + medição de fontes | onda 1 implantada | 2–4 h | R6.1 (medir) |
| R1 | lotes vazios → IGNORADO | R0, timers parados | 1 h (sim) + 30 min (apl.) | R6.3 (rotação) |
| R2 | CPF-por-nome → revisão | R1 | 1 h + 1 h | R6.3 |
| R3 | descartes suspeitos → revisão | R2 | 2 h + 1 h | — |
| R4 | retry dirigido | R3, disjuntor fechado, autorização de rede | 1 dia em ondas de 20 req | — |
| R3b | reavaliação final do dedup | R4 | 2 h | R5.1 |
| R5 | integridade em disco | R4 | 3–4 h | R6.1 |
| R6 | saneamento, retenção, descarte | R6.3 + decisão jurídica | 1 semana (quarentena) | R4/R5 |

\* estimativa de engenharia, **não medição**; o volume real vem do baseline.

**Portão entre fases:** cada fase termina com `inventario_estado.py --json` novo, comparação com o anterior e registro. Sem portão, uma fase mascara o efeito da outra.

---

## 11. Matriz de autorização

| Ação | Autoriza | Forma |
|---|---|---|
| Leitura e simulação em produção (R0, R1/R2/R3 sim) | responsável técnico | registro no livro; avisar operação |
| Parar timers / abrir janela | responsável técnico | aviso à operação, horário combinado |
| Marcar `IGNORADO`, `revisao_duplicata`, devolver à revisão | responsável técnico + operação do painel | lista de simulação aprovada |
| Retry de download em volume (R4) | sócio (ou técnico com autorização escrita) | orçamento de requisições + janela definidos |
| Mover arquivos de lote para quarentena | responsável técnico | manifesto da quarentena |
| Apagar quarentena / arquivos de cliente | **sócio** | 7 dias após a quarentena, por escrito |
| Podar/descartar backups com senha | **sócio + jurídico** | registro LGPD preenchido |
| Comunicação a titulares / ANPD | **sócio + jurídico** | decisão registrada com data |
| Rotação de webhook/chaves | sócio/TI | checklist de S06 |

Autorização verbal não conta: e-mail/chat registrado, anexado ao livro de execução.

---

## 12. Livro de execução (modelo)

| Campo | Exemplo de preenchimento |
|---|---|
| data/hora (Cidade Exemplo) | 2026-11-12 09:10 |
| item | R1 |
| operador | nome |
| autorização | e-mail de … em … |
| `sha256` do estado antes / depois | `…` / `…` |
| comando de simulação + saída | `r1-candidatos.txt` (n) |
| comando de aplicação + conflitos | `atualizar_lotes` → `{}` |
| contagem por estado lógico antes → depois | IGNORADO 0 → 812; REVISAO 1.349 → 1.349 |
| incidentes / desvios | nenhum |
| rollback acionado? | não |

Guardar em `/root/recuperacao/livro-<data>.md`, `0600`. É a fonte da avaliação LGPD e do relatório pós-operação.

---

## 13. Critérios de parada globais

Parar **tudo** e escalar se qualquer um ocorrer:

1. `EstadoCorrompido` ou o ciclo "começar do zero" (A2).
2. `atualizar_lotes` devolver conflito que não seja explicado por outra frente escrevendo.
3. Divergência > 1% entre a contagem da simulação e a do momento da aplicação.
4. `sha256` do estado mudar entre simular e aplicar sem execução registrada.
5. Disjuntor abrindo durante R4/R5, ou 429/503 em série.
6. Qualquer sinal de que a origem ainda grava senha (R6) — descarte suspenso.
7. Operação do painel relatando contrato/pasta gerado a partir de lote marcado por este plano.

---

## 14. Métricas de saída (o que "recuperado" significa)

| Métrica | Meta |
|---|---|
| lotes ativos sem arquivo e sem identidade | 0 (todos `IGNORADO` ou justificados em revisão) |
| lotes com `cpf_origem="bitrix-nome"` sem motivo de revisão | 0 |
| descartes por duplicata com download incompleto não reavaliados | 0 |
| arquivos pendentes com `erro_tipo` classificado | 100% (permanente ≠ transitório) |
| arquivos `.parcial`/`.part`/0 byte em `lotes/` | 0 |
| lotes com dois metas no mesmo `caminho` sem revisão | 0 |
| backups do estado retidos | ≤ 24 + 14 diários |
| lotes com marcador de senha no texto | 0 |
| lotes enviados ao painel com arquivo não baixado | 0 (F2-02 implantado) |

---

## 15. Riscos e o que só o servidor confirma

**Riscos do próprio plano**

- **R1 cedo demais** esconde kit legítimo sem arquivo (bug de download). Mitigação: critério 2–6 e o portão de R4 antes de aplicar em lote grande.
- **R3 marca duplicata real como suspeita** e enche a fila de revisão. Mitigação: taxa de candidatos > 10% = parar; reavaliação só depois do download completo.
- **R2 gera trabalho manual** (lotes voltam à revisão). É o preço de não manter CPF de homônimo no contrato.
- **R6 descarte cedo** destrói a evidência e o problema volta no próximo ciclo.
- **Escrita concorrente**: aplicar com timers rodando garante perda silenciosa. A janela não é opcional.

**A confirmar no servidor (o plano não presume)**

- Quantos lotes realmente têm senha no texto e quantos já foram liberados por `cpf_origem="bitrix-nome"` (CONTEXTO §7, 00-RESUMO §7).
- Quantos dos 503 descartes caíram no caso de subconjunto parcial.
- Quantos `.parcial`/0 byte existem em `lotes/` e quantos lotes têm dois metas no mesmo caminho.
- Conteúdo de `circuit-download.json`, journal das 13:06 e se o disjuntor fecha com sonda.
- Units reais: usuário, `UMask`, `PrivateTmp`, qual comando cada timer roda, e se `pipeline.sh` roda no timer de 10 min.
- Se existe cópia do `estado.json`/backups fora do servidor (WSL, Windows, sincronização) — muda o escopo do incidente LGPD.
- Se o painel precisa do campo de senha na fichinha (decide sanear × cifrar).

---

## 16. Pendências para outras frentes

| Pendência | Para quem | Por quê |
|---|---|---|
| `seguranca.remover_senhas` no caminho de persistência (`triagem_grupo`, `lote_para_job`) | frente da fase 1 / segurança | sem isso, R6 não tem efeito duradouro |
| `baixar_pendentes`/`download_svc`: memória de tentativas, `erro_tipo`, backoff em minutos, `.part`+rename, teto de 20 MB do bot | frente de download | R4 hoje depende de script operacional externo |
| ponte recusar lote com download incompleto (F2-02) e dedup só com kit completo (F2-03) | frente da ponte | sem isso, R3/R5 voltam a produzir falso descarte |
| `completar_cpf_por_nome` com paginação + 2ª âncora, sem `pronto=True` | frente de identificação | R2 é remédio; a cura é o código |
| `pintar_linha`/`lote_para_fila` consultarem `cpf_confiavel` | frente de identificação/pintura | evita linha nova com CPF de homônimo (PL-03) |
| unit systemd não-root, `UMask=0077`, `PrivateTmp`, logrotate | frente de operação | reduz a superfície durante e depois da recuperação |
| empacotar as receitas de R1/R2/R3/R5 como `manutencao/*.py` com `--simular/--aplicar` | onda seguinte | hoje são receitas operacionais; virar script versionado reduz erro humano |
