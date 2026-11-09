# 04 — FASE 2 (ponte para a fila do painel) e deduplicação

Escopo: `lote_para_fila.py`, `lote_para_job.py`, `ponte_criterio.py`, `ponte-rodar.sh`,
`monitor_lotes.py`, `relatorio.py`, `dividir_lote_misto.py`, `avisar-primeiro.py`
(+ `pipeline.sh` e trechos de `triagem_grupo.py` necessários para entender `pronto`).
Código: cópia local `~/Downloads/triagem-codigo-20261109-1552` (somente leitura).

## Como a ponte decide hoje (resumo verificado)

1. `prontos = pronto==True` (`lote_para_fila.py:538`). Na fase 1, `pronto` só é True com
   `identificacao=="OK"` (`triagem_grupo.py:2115-2141`) → SEM_CPF / CONFLITO / SEM_ARQUIVOS
   **nunca saem da fase 1 como pronto**. `requer_revisao` = identificação≠OK ou barreira
   (`triagem_grupo.py:2140`).
2. Regra do escritório `ponte_criterio.motivo_recusa` (`ponte_criterio.py:24-59`): recusa não-pronto,
   `arquivos` vazio, CONFLITO, `requer_revisao`, nome ruído; lote com traço passa **sem checar CPF**;
   fallback precisa 1 CPF + identificação OK + não "outro cliente".
3. Corte `--dias 7`, dedup por CPF+sha256 contra `fila-enviados.json`, CPF repetido na rodada
   (fica o de mais arquivos), `--limite` (timer: 4).
4. Staging (cópia + sidecar) → `node` + `FilaJobs.enfileirar` → controle `fila-enviados.json`.

Resposta direta às perguntas:
- (a) Lote em revisão **não** vai pelo timer (`ponte-rodar.sh`). **Vai** por `pipeline.sh`
  (`--todos-os-fechamentos` desliga a regra) e por `--lote` manual. SEM_CPF/CONFLITO/SEM_ARQUIVOS
  não vão porque não são `pronto`; lote com `arquivos` só de placeholder (nada baixado) passa
  na regra e é pulado depois ("nada a enviar") — ver F2-03.
- (b) Critério = **subconjunto**: descarta se todos os sha256[:12] *baixados* do lote já foram
  enviados para o mesmo CPF (união de todos os envios anteriores). Não compara entre CPFs
  (mesmo RG de outro cliente não é descartado). Kit + 1 doc novo → reenvia o kit **inteiro**.
- (c) Escrita na fila delegada a `FilaJobs` (tmp+rename segundo README; código do painel não
  está na cópia). Idempotência por id `grupo-<LOTE_ID>` + controle; quebra com concorrência
  e em falha no meio — ver F2-05/F2-06.
- (d) `upload_id` = `uuid4` novo a cada execução (não determinístico); sem colisão; sem limpeza
  em caminho de exceção.
- (e) Ordem lexicográfica por `lote_id` (LOTE-… antes de LOTE-SIG-…), limite aplicado antes de
  saber se o lote é enviável → bloqueio na cabeça da fila.
- (f) `lote_para_fila` reusa `montar_job` (não há cópia de lógica), mas a "simulação"
  `lote_para_job.py` **não aplica** nenhum filtro da ponte real — ver F2-09.

---

## Achados

### [F2-01] `pipeline.sh` enfileira sem a regra do escritório, sem corte de data, sem limite e sem trava
· Severidade: **ALTA**
· Arquivo: `pipeline.sh:15`; `lote_para_fila.py:522-523,545,564,708`
· Evidência: `python3 lote_para_fila.py --enviar --todos --todos-os-fechamentos 2>&1 | tail -20`
  — `--todos-os-fechamentos` põe `so_traco=False` (pula `lote_aceitavel`), `--dias` padrão 0
  (sem corte), `--limite` padrão 0 (sem limite), e não há `flock`.
· Impacto: quem roda o "pipeline manual" manda para o painel lote com nome ruído/1 palavra,
  fallback com >1 CPF, "cabeçalho de outro cliente" e qualquer `pronto && requer_revisao`
  (pela aritmética do CONTEXTO, 1.433 prontos + 1.349 revisão em 2.722 lotes ⇒ ≥60 lotes
  com as duas marcas). Sem `--dias`, o **backlog inteiro** de prontos não enviados entra numa
  rodada só. Sem `flock`, roda em paralelo com o timer da ponte (F2-05). O CONTEXTO descreve o
  timer `triagem-lote-fila` (10 min) com exatamente a sequência deste script + "enfileira até
  15 lotes": se o unit chama algo equivalente a este passo 4, a regra está desligada em
  produção a cada 10 min.
· Correção: remover `--todos-os-fechamentos` do `pipeline.sh`; usar os mesmos parâmetros do
  timer (`--dias 7 --limite N`) e envolver em `flock -n /var/lock/triagem-bitrix-ponte.lock`.
  Melhor: o passo 4 chamar `ponte-rodar.sh` em vez de `lote_para_fila.py` direto.
· Confiança: confirmado no código (`pipeline.sh`); o uso pelo timer de 10 min é **hipótese**
  (unit systemd não está na cópia).

### [F2-02] Kit com download incompleto é enfileirado parcial e o resto nunca mais é enviado
· Severidade: **ALTA**
· Arquivo: `lote_para_fila.py:206-209, 738-740, 540`; `ponte_criterio.py:30`; `triagem_grupo.py:2789-2792`
· Evidência: placeholder `{"baixado": False, "caminho": None, …}` entra em `arquivos`;
  `planejar` só pula (`"arquivo ausente no disco"`) e enfileira o resto; o lote só é recusado se
  **nenhum** arquivo existe (`if not sim["vai_enviar"]`). Depois de enviado, o lote vai para o
  controle e `alvos = [k … if a.forcar or k not in ctrl]` impede reenvio.
· Impacto: com o circuit-breaker aberto desde 09/11 13:06 e **453 arquivos não baixados**
  (370 adiados, 78 pausados), um lote pronto com parte dos arquivos baixada vai ao painel
  incompleto (contrato/pasta gerados sem RG/comprovante) e os arquivos que chegarem depois
  ficam só no disco. `pronto` não exige download completo (`triagem_grupo.py:2115-2141`), e a
  regra do escritório também não.
· Correção: em `motivo_recusa` recusar quando `any(not a.get("baixado") or not a.get("sha256")
  for a in arquivos)` → "download incompleto (N de M)", mantendo o lote fora até completar (o
  monitor já cobre "parado"). Opcional: após X horas, mandar parcial **com aviso explícito na
  fichinha** e registrar no controle os file_ids faltantes para um envio complementar.
· Confiança: confirmado no código.

### [F2-03] Dedup descarta como "duplicata" lote cujo download está incompleto
· Severidade: **ALTA**
· Arquivo: `ponte_criterio.py:67-86`; `lote_para_fila.py:624-650`
· Evidência: `hashes_do_lote` só conta arquivo com `sha256`; `kit_novo = bool(meus - antigos)`.
  O ramo de revisão só existe para `if not meus` (zero hashes). Se 2 de 5 arquivos baixaram e
  esses 2 já foram enviados antes para o CPF, o lote vira `descartado_duplicata` (não revisão),
  mesmo que os 3 não baixados sejam documentos novos.
· Impacto: é um caminho concreto para parte dos **503 descartados** não ser reenvio de verdade
  (cliente que reenvia RG + manda comprovante novo enquanto o download está pausado). O lote
  fica com `status=descartado_duplicata` e não volta a ser avaliado com os hashes completos sem
  `--lote` manual (o dedupe é refeito a cada rodada, mas a marca já saiu no Telegram como
  descarte).
· Correção: em `kit_novo`/no laço de `lote_para_fila.py:619`, se o lote tem arquivo sem sha256
  (placeholder ou erro), tratar como "indeterminado" → `revisao_duplicata` ou simplesmente
  adiar (não marcar nada) até o download completar. Testar com lote 2 baixados ⊆ anteriores + 1
  pendente.
· Confiança: confirmado no código.

### [F2-04] Bloqueio na cabeça da fila: limite aplicado antes de saber se o lote é enviável; LOTE-SIG sempre por último
· Severidade: **ALTA**
· Arquivo: `lote_para_fila.py:540, 693, 708-709, 738-740`; `ponte-rodar.sh:6,19`
· Evidência: `alvos = alvos[:a.limite]` (limite 4) sobre `sorted(...)` de `lote_id`; o lote sem
  arquivo utilizável cai em `continue` **sem** entrar no controle. Ordem lexicográfica:
  `"LOTE-2026…" < "LOTE-SIG-2026…"` (`'2' < 'S'`; ids em `triagem_grupo.py:2005`).
· Impacto: se os 4 lotes mais antigos da janela de 7 dias estão com 0 arquivo baixado (cenário
  atual: circuit-breaker aberto), **toda rodada gasta as 4 vagas neles e a ponte não entrega
  nada** até eles saírem da janela de 7 dias. Além disso, os lotes de traço (`LOTE-SIG`,
  confiança ALTA) só são atendidos depois de todos os de janela (`LOTE-`, confiança BAIXA —
  1.102 lotes); com backlog > 4 por ciclo, os de traço esperam.
· Correção: (1) aplicar o limite **depois** de `planejar` (contar só lotes com `vai_enviar`
  não vazio); (2) ordenar por `inicio`/`fim` (datetime) e não por id; (3) quem foi pulado por
  "nada a enviar" vai para o fim da fila da rodada seguinte (ou fica de fora se F2-02 for
  aplicada).
· Confiança: confirmado no código; impacto em produção depende de quantos lotes estão sem
  download agora (não medido).

### [F2-05] Duas instâncias da ponte em paralelo: perda de atualização no controle e reenvio
· Severidade: **MÉDIA** (ALTA se F2-01 roda no timer de 10 min)
· Arquivo: `lote_para_fila.py:93-97, 361-368, 537, 780`; `ponte-rodar.sh:9`; `pipeline.sh:15`
· Evidência: controle lido no início (`ctrl = carregar_controle()`) e regravado inteiro no fim
  (`gravar_controle(ctrl)`), sem trava; arquivos temporários fixos `/tmp/_enfileirar.js`,
  `/tmp/_entradas.json`, `fila-enviados.json.tmp`. Só `ponte-rodar.sh` usa `flock`.
· Impacto: duas execuções simultâneas (timer de 5 min + pipeline/timer de 10 min) → (i) a
  última a gravar apaga do controle os lotes que a outra registrou; quando o job desses lotes já
  saiu da fila (o FilaJobs guarda só 100 terminais — README:225), a próxima rodada **reenfileira
  o mesmo lote** (contrato/pasta processados de novo) e o dedup por CPF perde o histórico de
  hashes; (ii) uma instância pode ler `/tmp/_entradas.json` da outra.
· Correção: `fcntl.flock` exclusivo dentro de `main()` de `lote_para_fila.py` (mesmo arquivo
  `/var/lock/triagem-bitrix-ponte.lock`), recarregar o controle sob a trava antes de gravar
  (merge), e `tempfile.mkstemp` para os temporários.
· Confiança: confirmado no código; concorrência real é hipótese (depende dos units).

### [F2-06] Falha no meio deixa staging órfão e controle apontando para upload_id errado
· Severidade: **MÉDIA**
· Arquivo: `lote_para_fila.py:211, 253-262, 341-379, 762-781`; `lote_para_job.py:808`
· Evidência: `upload_id = str(uuid.uuid4())` a cada execução; staging montado para todos os
  lotes **antes** de `gravar_na_fila`; `subprocess.run(..., check=True)` e `json.loads(...)` sem
  `try` — exceção aborta antes de `gravar_controle` e de qualquer `limpar_staging`.
· Impacto:
  - node falha / fila ilegível → staging de até N lotes fica órfão para sempre (uuid novo na
    próxima rodada, ninguém limpa);
  - node grava a fila mas o Python cai depois → job existe com `upload_id=A`, controle não tem o
    lote; na rodada seguinte monta staging `B`, `enfileirar` responde "JÁ EXISTE" e o controle
    registra `upload_id=B` (`lote_para_fila.py:775-776`) — controle mente, `B` órfão;
  - `--forcar` com o job antigo ainda na fila: "JÁ EXISTE", controle sobrescrito, **nada é
    reprocessado** (hipótese: depende de `FilaJobs.enfileirar` devolver falsy para id existente,
    como o código assume em `criado: !!j`).
· Correção: `upload_id` determinístico (`uuid5(NAMESPACE, lote_id)` ou `grupo-<LOTE_ID>`), assim
  a re-execução reaproveita/sobrescreve o mesmo diretório; `try/except` em volta de
  `gravar_na_fila` que limpa o staging dos lotes cujo job não está na fila; registrar no
  controle o `upload_id` **lido do job na fila**, não o planejado; para `--forcar`, gerar id
  `grupo-<LOTE_ID>-r<n>`.
· Confiança: confirmado no código (exceto o comportamento interno de `FilaJobs`).

### [F2-07] Monitor nunca fica em silêncio: 503 descartados + todo `requer_revisao` reavisados a cada 30 min; "pronto" > 7 dias anunciado como "encaminha no próximo ciclo"
· Severidade: **MÉDIA**
· Arquivo: `monitor_lotes.py:113-119, 129-140, 57, 211-239`; `ponte-rodar.sh:19`
· Evidência: entra todo lote `descartado` ou `requer` (inclusive sem arquivo:
  `if not arquivos and not (descartado or requer)`), sem dedupe de alerta; rótulo
  `"pronto": "limpo(s) — a ponte encaminha no próximo ciclo"` sem considerar o corte `--dias 7`.
· Impacto: com 503 descartados e 1.349 em revisão (615 SEM_ARQUIVOS, ~900 vazios), a saída do
  cron nunca é vazia → Telegram toda meia hora com o mesmo texto; o aviso real (lote limpo
  parado, sem motivo anotado) se perde no ruído. Lote limpo com mais de 7 dias é anunciado como
  "vai no próximo ciclo" e nunca vai.
· Correção: (1) excluir `descartado_duplicata` já alertado (o próprio `marcar_duplicatas_no_estado`
  já alerta 1 vez) e lotes sem arquivo; (2) dedupe por conjunto de lotes (só imprimir quando o
  conjunto muda, como `avisar-primeiro.py` faz com `erros_avisados`); (3) separar
  "pronto fora da janela da ponte (> 7 dias) — precisa catch-up `--dias 0`".
· Confiança: confirmado no código.

### [F2-08] Kit repetido + 1 documento novo reenvia o kit inteiro; dedup é contra a união de todo o histórico do CPF
· Severidade: **MÉDIA**
· Arquivo: `ponte_criterio.py:77-86`; `lote_para_fila.py:617, 625, 201-209`
· Evidência: `kit_novo` → True se **algum** hash é novo; `planejar` só tira duplicata **dentro**
  do lote (`duplicado_de`), não contra o que já foi enviado. `hashes_por_cpf` acumula todos os
  envios, sem data.
· Impacto: (i) o painel recebe de novo RG/CPF/comprovante já processados (pasta/NAS com
  arquivos duplicados, IA reprocessa); (ii) risco baixo de descarte legítimo: um caso novo do
  mesmo cliente meses depois só com documentos já enviados (sem kit assinado novo) vira
  `descartado_duplicata`. Não há descarte entre CPFs diferentes (mesmo RG reenviado por outra
  cliente **não** é descartado — correto). Truncar em 12 hex (48 bits) não traz risco prático
  de colisão.
· Correção: no envio de um 2º kit do mesmo CPF, enviar só `meus - antigos` (e citar na fichinha
  "complemento do lote X"), ou ao menos marcar no job os arquivos já enviados; limitar a
  comparação a envios dos últimos N dias (ex.: 90).
· Confiança: confirmado no código; efeito no painel (pasta duplicada) é hipótese.

### [F2-09] `lote_para_job.py` ("simular a ponte") diverge da ponte real e faz rede
· Severidade: **MÉDIA**
· Arquivo: `lote_para_job.py:960-1017, 387-435, 896-911`; CONTEXTO §9
· Evidência: `main()` de `lote_para_job.py` pega **todos** os `pronto` sem `ponte_criterio`,
  sem `--dias`, sem dedup CPF/sha, sem limite, sem `fichinha_sem_vazios`, com id `uuid4`; e
  `montar_job` chama `codigo_pasta_do_cliente` → `POST crm.contact.list` no Bitrix por lote.
· Impacto: o comando que o CONTEXTO recomenda para "simular a ponte (não escreve nada)" mostra
  lotes que a ponte real recusaria e não mostra os descartes — decisão operacional em cima de
  dado errado; e a "simulação" consome a API do Bitrix (que está bloqueando — circuit aberto).
  A simulação fiel é `lote_para_fila.py --simular --todos --dias 7 --limite 4` (que também faz a
  chamada ao Bitrix via `montar_job`).
· Correção: `lote_para_job.py main()` delegar a `lote_para_fila.main(["--simular", …])` ou
  imprimir aviso "não aplica filtros da ponte"; flag `--sem-rede` que pula
  `codigo_pasta_do_cliente`; corrigir docstring (`lote_para_job.py:8-10` ainda diz "worker
  parado"; README:129 idem).
· Confiança: confirmado no código.

### [F2-10] Job real sai com histórico "SIMULAÇÃO — não gravado" e sidecar com `id` vazio
· Severidade: **BAIXA**
· Arquivo: `lote_para_job.py:830-838, 911`; `lote_para_fila.py:195-199, 215, 260`
· Evidência: `planejar` só sobrescreve `fichinha` e `id`; `historico=[{"evento": "enfileirado
  (SIMULAÇÃO — não gravado)"}]` vai para a fila real. `montar_job` não copia `file_id` para
  `arquivos`, então `a.get("file_id")` é sempre `None` → nome staged = `sha256[:12].ext` e
  sidecar `{"id": ""}`.
· Impacto: histórico do painel enganoso em todo job da ponte (a menos que `FilaJobs.enfileirar`
  reescreva — hipótese); o worker perde o vínculo com o file_id do Bitrix. Os 450 arquivos sem
  extensão viram `.bin` / `application/octet-stream`; áudio/vídeo recebem MIME e entram no
  staging (`lote_para_fila.py:63-73`) — confirma o problema 4 do CONTEXTO.
· Correção: em `planejar`, `historico=[{"em": agora(), "evento": "enfileirado pela ponte do
  grupo"}]`; incluir `"file_id": a.get("file_id")` no dict de `montar_job`; filtrar áudio/vídeo
  antes do staging (ou mandar com marcação "não documento").
· Confiança: confirmado no código.

### [F2-11] `dividir_lote_misto.py` cria sub-lote pronto sem mexer no pai — risco de envio duplo
· Severidade: **BAIXA**
· Arquivo: `dividir_lote_misto.py:121-151, 118-120`
· Evidência: sub-lote = `dict(lote)` + `pronto=True, requer_revisao=False, identificacao="OK"`;
  o pai fica intocado (sem `dividido_em`/status). Bloco sem CPF no kit: `continue` (arquivos
  do bloco não vão para lugar nenhum). `lote_id` do sub-lote embute o CPF (`f"{lid}-{cpf}"`),
  que vira id do job `grupo-…-<CPF>`.
· Impacto: se o pai depois virar `pronto` (ex.: `completar_cpf_por_nome` acha CPF), ele vai
  com **todos** os arquivos misturados, além dos sub-lotes (o dedup só pega se o CPF for o
  mesmo de um sub-lote). CPF em claro no id do job/log.
· Correção: marcar o pai com `status="DIVIDIDO"`, `pronto=False`, `dividido_em=[…]` no mesmo
  `mutador`; usar índice/hash curto no id do sub-lote em vez do CPF.
· Confiança: confirmado no código; reavaliação do pai é hipótese.

### [F2-12] Robustez menor
· Severidade: **BAIXA**
· `lote_para_job.py:401-405` — `from nomear_lotes_por_cpf import WEBHOOK` executa
  `raise SystemExit` (`nomear_lotes_por_cpf.py:66,69`) se o webhook não estiver nos configs;
  `except Exception` não pega `SystemExit` → a ponte inteira morre em vez de seguir sem código
  de pasta. Correção: `except BaseException` ou mover o lookup para função sem `SystemExit`.
· `lote_para_fila.py:341-344` — backup `fila-jobs-<data>.json` a cada rodada com entrada, sem
  retenção (mesmo padrão dos 27 GB do estado, aqui com arquivo pequeno).
· `avisar-primeiro.py:101-118` — lote do controle cujo job já saiu da fila **e** do espelho vira
  `j={}` e nunca gera aviso (falha silenciosa); `erros_avisados` cresce sem limite.
· `relatorio.py:366-372` — o desfecho "descartado"/"em revisão" da ponte não é emitido para o
  grupo (o próprio comentário diz que a chamada "ainda NÃO está plugada"); o grupo só vê
  "Lote registrado".
· Confiança: confirmado no código.

---

## Testes executados

`python3 -m pytest testes/<arquivo> -q -p no:cacheprovider` (sem cache):

| Arquivo | Resultado | Observação |
|---|---|---|
| test_ponte_criterio | 19 passed | — |
| test_relatorio | 26 passed | — |
| test_limpeza_servico | 1 passed | — |
| test_ponte_fila | 8 passed, 1 failed | falha = ambiente: lê `/opt/agente-separador/src/fila-jobs.js` |
| test_monitor_lotes | 15 passed, 1 failed | falha = ambiente: CLI lê `/opt/triagem-bitrix/estado.json` (rc 2) |
| test_servico_lote | 3 failed | ambiente: `/opt/triagem-bitrix/estado.json` e fixture `com_servico.pdf` ausentes |
| test_variaveis_contrato | 7 failed | ambiente: `/opt/triagem-bitrix/estado.json` ausente |

Nenhuma falha é de lógica; todas dependem de caminho do servidor. Lacunas de teste ligadas aos
achados: nenhum teste cobre lote parcialmente baixado (F2-02/F2-03), ordem/limite (F2-04),
concorrência/controle (F2-05) nem falha entre staging e fila (F2-06).

## Top 5 (prioridade)

1. **F2-01** — `pipeline.sh` enfileira com `--todos-os-fechamentos`, sem `--dias`/`--limite`/`flock`
   (fura a regra do escritório; possivelmente é o timer de 10 min).
2. **F2-02** — kit com download incompleto vai parcial para o painel e o restante nunca é enviado.
3. **F2-03** — dedup marca `descartado_duplicata` em lote com download incompleto (falso descarte).
4. **F2-04** — limite antes do "enviável" + ordem por id: 4 lotes sem download travam a ponte; LOTE-SIG por último.
5. **F2-05/F2-06** — controle sem trava e `upload_id` aleatório: corrida apaga controle e reenvia; falha no meio deixa staging órfão/controle errado.

## O que NÃO deu para verificar sem o servidor

- Units systemd (`triagem-lote-fila`, `triagem-bitrix-ponte`): de onde vem o "até 15 lotes",
  se o de 10 min chama `pipeline.sh`/`--todos-os-fechamentos`, e se usa o mesmo lock.
- `/opt/agente-separador/src/fila-jobs.js`: se `enfileirar` é realmente atômico/merge por id,
  o que devolve para id existente (base de F2-06 `--forcar`), se reescreve `historico`, e quem
  limpa `staging/<upload_id>/` depois do job.
- Quantos dos 503 descartados tinham arquivo sem sha256 na hora (F2-03) e quantos lotes da
  janela de 7 dias estão sem download agora (F2-04).
- Conteúdo de `fila-enviados.json` (se já houve perda de registro por corrida) e diretórios
  órfãos em `staging/`.
