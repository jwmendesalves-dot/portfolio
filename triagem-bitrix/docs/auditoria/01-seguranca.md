# 01 — Segurança, segredos e dados pessoais (LGPD) — triagem-bitrix

Escopo: todos os `.py`, `.sh` e `config.json` de `Downloads/triagem-codigo-20261109-1552` (cópia de `/opt/triagem-bitrix`). Somente leitura; nenhum script executado contra a rede. Nenhum valor de segredo, CPF ou senha foi copiado para cá: cito só arquivo:linha e o tipo.

Resumo: **não há token, webhook ou X-Api-Key hardcoded** nos arquivos do escopo (varredura por regex de `rest/N/<chave>`, token Telegram `NNN:AA…` e `X-Api-Key: "<valor>"` não achou nada). Os problemas graves estão em outro lugar: (1) **senha gov.br/Meu INSS dos clientes** é extraída, gravada e copiada sem controle (estado, backups, fila); (2) **o bot do Telegram aceita documento de qualquer pessoa** e o que chega vai para a produção; (3) a credencial do Bitrix (webhook do usuário 1) é **descoberta varrendo arquivos de outros projetos**, uma das fontes fica em `/tmp`, e há caminhos em que ela vai para o journal ou para o `ps`; (4) não existe retenção nem permissão restrita em nada que guarda CPF.

---

## CRÍTICA

### [S01] Senha gov.br / Meu INSS do cliente é extraída e replicada em estado, backups e fila sem proteção
- **Severidade:** CRÍTICA
- **Arquivo:linha:** `lote_para_job.py:85,114,233-239,304,364-371,489,493` (parse e montagem); `lote_para_job.py:60` (template da fichinha `• **NIS/Senha do INSS:**{nis_senha}`); `triagem_grupo.py:2547` (`"texto": m["texto"][:2000]` grava o texto bruto de cada mensagem no `estado.json`); `completar_cpf_por_nome.py:141-142`, `nomear_lotes_por_cpf.py:206-207`, `reconciliar_arquivos_disco.py:109-110`, `reparo_josilene.py:123-124`, `backfill_nome_lotes.py:86-87` (`shutil.copy2(ESTADO, f"{ESTADO}.bak-…")`); `lote_para_fila.py:341-343` (`backups/fila-jobs-<marca>.json` a cada envio); `lote_para_fila.py:363-364` (`/tmp/_entradas.json` com os jobs).
- **Evidência:** `CAMPOS_FICHA = [..., "cpf", ..., "email", "senha", ...]`; `"senha": ["acesso ao govbr senha", "acesso ao gov br cpf e senha", "senha gov br", "senha"]`; `nis_senha=_nis_e_senha(campos.get("nis"), campos.get("senha"))`.
- **Impacto:** a senha de acesso ao gov.br do cliente (que dá acesso a INSS, CNIS, Receita, benefícios) fica em texto puro: (a) no `estado.json` (15 MB) dentro de `mensagens[].texto`; (b) em **cada um dos 1.833 `estado.json.bak-*` (27 GB)**, que nunca são apagados; (c) na `fila-jobs.json` do painel e em cada `backups/fila-jobs-*.json` (737 MB em `backups/`); (d) em `/tmp/_entradas.json`, que é sobrescrito mas fica no disco. Basta um vazamento de qualquer cópia (backup, disco do WSL, sincronização do Windows) para expor a credencial gov.br de centenas de clientes. Para um escritório de advocacia, isso é incidente de segurança reportável à ANPD e gera responsabilidade civil.
- **Correção:** (1) não persistir a senha: na triagem, trocar o valor por marcador (`senha: [informada]`) antes de `serializar_lote` e antes de montar a fichinha; se o painel precisar dela, cifrar o campo (por exemplo, `age`/`libsodium` com chave fora do disco de dados) e passar só no job; (2) script único que remove a senha de `estado.json`, `*.bak-*` e `backups/fila-jobs-*` (ou apaga os backups, ver S05); (3) registrar a decisão no RIPD.
- **Confiança:** confirmado no código (o fluxo de dados). Não verifiquei quantos lotes reais contêm senha (exige o servidor).

### [S02] Credencial de cliente (e-mail + senha) e CPFs válidos hardcoded no código e nos testes
- **Severidade:** CRÍTICA (a senha) / ALTA (os CPFs)
- **Arquivo:linha:** `lote_para_fila.py:435` (dicionário de autoteste com `"email": "<e-mail real>"` e `"senha": "<senha>"`); `lote_para_fila.py:427-433` (fixture com e-mail e 2 telefones de uma cliente, "lote 19/09"). CPFs com dígito verificador válido: `triagem_grupo.py` (7), `lote_para_fila.py`, `backfill_nome_lotes.py`, `certidoes.py:24`, `relatorio.py`, `nome_regras.py`, `README.md` (2) e 14 arquivos em `testes/` (no total, cerca de 27 CPFs válidos). `reparo_josilene.py` tem o nome de uma cliente no nome do arquivo.
- **Evidência:** a varredura com o cálculo do dígito verificador encontrou 27 CPFs distintos que passam no DV (não copiados aqui).
- **Impacto:** dado pessoal e credencial de cliente dentro do código-fonte, que circula em cópias (como esta, em `Downloads/`), em zip, em git e em agentes de IA. A senha parece ser de uma cliente real (o formato e o e-mail pessoal indicam isso).
- **Correção:** substituir por dados sintéticos (CPF gerado com DV válido e marcado como fictício, e-mail `@example.com`, senha fictícia); expurgar do histórico do git, se houver repositório; tratar a senha como comprometida e avisar a cliente para trocá-la.
- **Confiança:** confirmado que os valores estão no código e que os CPFs passam no DV. Que pertencem a pessoas reais é **hipótese** forte.

### [S03] Telegram: `chats_permitidos=[]` libera QUALQUER pessoa para mandar arquivo ao bot, e o arquivo entra na produção
- **Severidade:** CRÍTICA
- **Arquivo:linha:** `config.json` (`telegram.habilitado = true`, `telegram.chats_permitidos = []`); `telegram_regras.py:45-48`; `telegram_grupo.py:166`, `:183-195`, `:296-306`, `:447-448`; nenhum filtro por origem em `ponte_criterio.py`, `lote_para_fila.py` ou `lote_para_job.py` (grep por `tg:|telegram|fonte_entrada|dialog_id` vazio).
- **Evidência:** `return True if not permitidos else str(chat_id) in permitidos` (docstring: "Lista vazia = aceita qualquer chat"). `processar()` baixa os anexos para `lotes/<LOTE>/` e `mesclar_no_estado` grava o lote no mesmo `estado.json` que a ponte lê. A ponte trata o lote igual a um lote do Bitrix.
- **Impacto:** quem descobrir o @ do bot (o nome é previsível pelo token file `documentosexemplo`) consegue: (a) injetar documentos e fichinhas falsas que viram job no painel e contrato (fraude ou contrato em nome de terceiro com CPF digitado na legenda); (b) mandar arquivos arbitrários (sem limite de extensão, ver S10) que o worker e a IA vão abrir; (c) encher o disco. `decidir_respostas` (`:207-262`) também responde em qualquer chat, sem passar pelo filtro: o bot confirma o recebimento para estranhos.
- **Correção:** inverter a regra para fail-closed: lista vazia passa a significar NENHUM chat (`return str(chat_id) in permitidos`), preencher `chats_permitidos` com os IDs da equipe, aplicar o mesmo filtro em `decidir_respostas`/`acumular_buffer`, e marcar `fonte_entrada="telegram"` + `requer_revisao=True` na ponte até haver allowlist. Se o canal Telegram é legado, desligar com `telegram.habilitado=false` e parar o serviço.
- **Confiança:** confirmado no código. Não sei se o processo `telegram_grupo.py --loop` está rodando hoje (não aparece entre os 4 timers do CONTEXTO; verificar o `systemctl` no servidor). Se estiver, a exposição é imediata.

---

## ALTA

### [S04] CPF, nome do cliente e nome original dos arquivos vão para o journal e para o `ponte.log` a cada 5 min
- **Severidade:** ALTA
- **Arquivo:linha:** `lote_para_fila.py:231` (`print(f"  cliente     : nome={…!r}  cpf={…!r}")`), `:238` (nome original do anexo), chamado em modo `--enviar` em `:737`; `:558`, `:669`, `:672` (CPF em descarte de duplicata); `ponte-rodar.sh` (stdout do timer de 5 min); `completar_cpf_por_nome.py:124` (`{nome} -> {cpf}`, no `pipeline.sh` a cada 10 min); `nomear_lotes_por_cpf.py:197`; `certidoes.py:330` (`cpf=… nome=…`, timer de 5 min); `telegram_grupo.py:442`; `lote_para_job.py:940`.
- **Impacto:** o `ponte.log` (208 MB, sem rotação) e o journal acumulam nome + CPF de todo cliente processado, sem prazo de expiração. Isso cria uma segunda base de dados pessoais, fora do controle de acesso e de retenção. Quem tem acesso a log (`journalctl` costuma ser legível pelo grupo `adm`/`systemd-journal`) vê a carteira de clientes.
- **Correção:** registrar só `lote_id` + CPF mascarado (`***.456.789-**`) + contagem; mover os detalhes para `--verbose`; configurar `logrotate` no `ponte.log` (diário, `rotate 14`, `compress`, `create 0600 root root`) e `MaxRetentionSec` no journald.
- **Confiança:** confirmado no código. O destino do stdout (journal ou `ponte.log`) depende da unit, que não está na cópia.

### [S05] Retenção inexistente: 1.833 cópias do estado com CPF, senha e mensagens (27 GB), mais os backups da fila
- **Severidade:** ALTA
- **Arquivo:linha:** `completar_cpf_por_nome.py:126,141-142` (backup a cada `--aplicar` com achados; roda no `pipeline.sh:13` a cada ciclo de 10 min); `reconciliar_arquivos_disco.py:109`, `nomear_lotes_por_cpf.py:206`, `reparo_josilene.py:123`, `backfill_nome_lotes.py:86`; `lote_para_fila.py:341-344` (`backups/fila-jobs-*.json` a cada envio). Nenhum código apaga backups (grep por `remove|unlink|rmtree` em backups: nada).
- **Impacto:** cerca de 2,1 GB/dia de cópias integrais com nome, CPF, telefone, e-mail, texto das conversas e senha gov.br (S01). Isso contraria a necessidade e a eliminação previstas nos arts. 6º, III e 16 da LGPD, e multiplica a superfície de vazamento. Também ameaça o disco (risco operacional).
- **Correção:** centralizar o backup em `estado_store` com retenção (por exemplo, manter os últimos 24 + 1 por dia por 7 dias), gravando em `backups/` com `0600`; política idêntica para `fila-jobs-*`; limpeza única dos existentes depois de sanear (S01). Política de retenção dos `lotes/` (4,9 GB de documentos) depois de o job ser concluído no painel.
- **Confiança:** confirmado no código (ausência de retenção). O número de 27 GB vem do CONTEXTO.

### [S06] O webhook do Bitrix (usuário 1, provável admin) é descoberto varrendo arquivos de outros projetos, e uma das fontes fica em `/tmp`
- **Severidade:** ALTA
- **Arquivo:linha:** `triagem_grupo.py:136-141` + `:407-433` (lê até 400 arquivos `.py/.json/.env/.txt/.yml/.sh` de `/opt/data/mcp/bitrix24_mcp_server.py` e da árvore `/root/trampo/PrevSync-CRM` em busca de `https://…/rest/1/<chave>/`); `download_svc.py:33` (`B24_WEBHOOK_FILE = "/tmp/b24_webhook.txt"`); `baixar_pendentes.py:26` (`/opt/leads-doenca/config.json`, de outro projeto); `certidoes.py:45` (`/opt/agente-separador/config.json`); `nomear_lotes_por_cpf.py:37-66` (varre `.env`/`config.json` de 2 projetos). A chave VibeCode existe em dois arquivos: `vibecode.key` (`triagem_grupo.py:86`, `certidoes.py:43`, `vigia-bot-triagem.py:29`) e `/root/.vibecode/api_key` (`download_svc.py:34`, `esperar_bot_liberar.py:20`).
- **Impacto:** (a) prova que a URL com chave está **hardcoded em código-fonte de outros projetos** (`bitrix24_mcp_server.py` e o repositório PrevSync-CRM), fora deste escopo mas no mesmo servidor; (b) `/rest/1/` é o webhook do usuário ID 1, normalmente o administrador do portal, com escopo CRM, disco e IM completo: quem o obtém lê e altera todo o CRM do escritório; (c) `/tmp/b24_webhook.txt` está num diretório gravável por todos. Se o arquivo não existir, outro usuário local pode criá-lo apontando para um host próprio, e o `download_svc` passa a baixar o conteúdo que esse host devolver como se fosse documento do cliente (injeção na produção). Se existir com a umask padrão, fica legível por todos; (d) rotacionar a chave exige caçar 6 a 7 lugares diferentes.
- **Correção:** uma fonte única, `/etc/triagem-bitrix/segredos.env` (`0600 root:root`), carregada pela unit com `EnvironmentFile=` (ou `LoadCredential=`); remover a descoberta por varredura (`candidatos_webhook`) e `/tmp`; criar um webhook dedicado a um usuário técnico com escopos mínimos (`im`, `disk`, `crm` só leitura onde der) em vez do usuário 1; rotacionar o webhook atual e remover a URL do `bitrix24_mcp_server.py`/PrevSync-CRM.
- **Confiança:** confirmado no código. Que o usuário 1 é admin e que o webhook está em texto nos outros projetos é **hipótese** forte (o regex procura exatamente isso).

### [S07] Webhook completo (com a chave) escrito no log quando um candidato falha
- **Severidade:** ALTA
- **Arquivo:linha:** `triagem_grupo.py:450` e `:452` (`log.warning("webhook %s respondeu sem 'result.ID'…", u)` / `log.warning("webhook %s não validou (%s)…", u, e)`). O log vai para o `RotatingFileHandler` `ARQ_LOG` e para o stderr, ou seja, o journal (`:219-229`).
- **Evidência:** `u` é a URL inteira `https://…/rest/1/<chave>/`. Na linha 448 (sucesso) o código mascara (`u.split("/rest/")[0]`), mas nos dois caminhos de falha não mascara.
- **Impacto:** toda vez que um webhook velho ou revogado ainda estiver nos arquivos varridos (S06), ou que o portal cair, a chave do webhook vai para o journal e para o arquivo de log. `http_json` cai no mesmo problema se algum chamador futuro omitir `rotulo` (`:327,335,342,346,349,352` usam `rotulo or url`); hoje todos os chamadores do webhook passam `rotulo`.
- **Correção:** função `mascarar(url)` (`re.sub(r"/rest/(\d+)/[^/]+/", r"/rest/\1/***/", url)`) aplicada em `:450`, `:452` e no `rotulo or url` de `http_json`; depois, limpar os logs antigos e rotacionar o webhook se encontrar a chave neles.
- **Confiança:** confirmado no código. Não verifiquei se já houve vazamento no journal (exige o servidor).

### [S08] Todos os serviços rodam como root, sem permissão restrita nos arquivos com dados pessoais
- **Severidade:** ALTA
- **Arquivo:linha:** leitura de `/root/.telegram/…token` (`telegram_grupo.py:38`), de `/root/.vibecode/api_key` (`download_svc.py:34`), node em `/root/.hermes/node/bin/node` (`lote_para_fila.py:53`, `certidoes.py:206`), `systemctl restart/start` (`vigia_bot.sh:32`, `esperar_bot_liberar.py:33`), locks em `/var/lock` (`ponte-rodar.sh:9`, `certidoes-rodar.sh:6`). Nenhum `chmod`/`umask` no código (grep vazio); os locks são criados `0o644` (`estado_store.py:83`, `circuit_breaker.py:141`, `triagem_grupo.py:4037`, `pergunta_traco.py:604`).
- **Impacto:** `estado.json`, `*.bak-*`, `lotes/<LOTE>/*` (RG, CPF, certidões, comprovantes), `staging/`, `fila-jobs*.json`, `download_fila.json` e `telegram-estado.json` são criados com a umask do processo (padrão `022`, ou seja, `0644`, legível por qualquer usuário do sistema). Como tudo roda como root, qualquer falha (por exemplo, em parser de PDF/imagem no worker, ou o RCE em S09) vira comprometimento total do host, incluindo as credenciais de todos os projetos em `/root`.
- **Correção:** usuário de sistema `triagem` dono de `/opt/triagem-bitrix` e de `staging/`; nas units, `User=triagem`, `UMask=0077`, `ProtectSystem=strict`, `ReadWritePaths=/opt/triagem-bitrix /opt/agente-separador/data`, `PrivateTmp=yes`, `NoNewPrivileges=yes`; um `chmod -R go-rwx` único nos dados existentes; o reinício do bot via `sudoers` restrito ou `Restart=on-failure` + `WatchdogSec`, em vez de `systemctl` dentro do script.
- **Confiança:** confirmado que o código exige root (paths em `/root` e `systemctl`). As permissões reais e o `User=` das units são **hipótese** (as units não estão na cópia).

### [S09] Script JS gravado em `/tmp` com nome fixo e executado pelo node como root
- **Severidade:** ALTA (se houver outro usuário local ou serviço comprometido) / MÉDIA (se o host for monousuário)
- **Arquivo:linha:** `lote_para_fila.py:361-368` (`/tmp/_enfileirar.js` + `/tmp/_entradas.json` → `subprocess.run([NODE, "/tmp/_enfileirar.js", …])`); `certidoes.py:205-206` (`/tmp/_ler_certidao.js`); `certidoes.py:323-324` (`/tmp/certidao_<id>.pdf`, nunca apagado: grep `remove|unlink` em `certidoes.py` vazio).
- **Impacto:** (a) quem conseguir pré-criar ou trocar `/tmp/_enfileirar.js` entre a escrita e a execução (ou se `fs.protected_regular=0`) executa código como root a cada 5 min; (b) `_entradas.json` (jobs com fichinha, CPF e senha, S01) e as **certidões de nascimento de menores** em `/tmp/certidao_*.pdf` ficam no `/tmp` legíveis por todos e se acumulam; (c) `ponte-rodar.sh` (lock da ponte) e `pipeline.sh` (sem lock) executam `lote_para_fila.py` e disputam o mesmo `/tmp/_entradas.json`: uma rodada pode enfileirar os jobs da outra ou ler JSON truncado (**hipótese**, depende de qual unit chama `pipeline.sh`).
- **Correção:** `tempfile.TemporaryDirectory(prefix="triagem-", dir="/opt/triagem-bitrix/tmp")` com `0700`; o script JS como arquivo versionado em `/opt/triagem-bitrix/js/enfileirar.js` (não gerado em runtime); `os.remove` da certidão em `finally`; o mesmo `flock` da ponte no `pipeline.sh`.
- **Confiança:** confirmado no código (paths fixos e falta de limpeza). A exploração depende do sysctl e de haver outro usuário local: **hipótese**.

---

## MÉDIA

### [S10] Extensão do arquivo não é validada: qualquer tipo vai para `lotes/` e para o staging do worker
- **Severidade:** MÉDIA
- **Arquivo:linha:** `telegram_grupo.py:107-111` (`os.path.splitext(nome)[1].lower()` do `file_name` do remetente); `triagem_grupo.py:2236-2242` (ext do `NAME` do Bitrix quando o nome está ofuscado); `baixar_pendentes.py:70-72` (`nome_limpo` só troca `/` e `\`); `lote_para_fila.py:211-216` (`staged_nome = f"{fid}.{ext}"`, mime `application/octet-stream` para o que não está em `MIME_EXT`).
- **Path traversal:** **não encontrei.** `_nome_seguro` (`triagem_grupo.py:2170-2173`) faz `basename` + allowlist `[\w.\-]` + 120 caracteres; `baixar_pendentes` prefixa `NN_` (o que anula `..`); a extensão de `splitext` nunca contém `/`; `lote_id` e `upload_id` (`uuid4`, `lote_para_job.py:808`) são gerados internamente; o `file_id` do Telegram é base64url. Um ponto lateral: `_nome_seguro("..")` devolve `..`, o destino vira a pasta pai, o código acha que o arquivo existe e marca `reaproveitado` sem baixar (não grava fora, mas perde o documento em silêncio).
- **Impacto:** `.html`, `.svg`, `.exe`, `.js` e extensões longas chegam ao staging e ao painel (combinado com S03, por qualquer pessoa). Também há colisão de nome: dois anexos com o mesmo `NAME` não ofuscado no mesmo lote (por exemplo, `documento.pdf` duas vezes) caem no mesmo destino e o segundo é dado como `reaproveitado` (`triagem_grupo.py:2249-2251`), ou seja, documento perdido sem erro.
- **Correção:** allowlist de extensões (`jpg|jpeg|png|webp|heic|pdf`; mídia separada, ver CONTEXTO §7.4) validada também pelo magic number; nome salvo sempre `NN_<file_id>.<ext>` (o índice resolve a colisão); tratar `.`/`..`/vazio como `arquivo`.
- **Confiança:** confirmado no código.

### [S11] `completar_cpf_por_nome` atribui CPF por homônimo sem paginar o CRM e libera o lote direto para a produção
- **Severidade:** MÉDIA (LGPD/integridade: documento de uma pessoa vinculado ao CPF de outra)
- **Arquivo:linha:** `completar_cpf_por_nome.py:55-75` (`crm.contact.list` com `filter[%NAME]` = **primeiro nome**, sem `start`, então só os primeiros 50 resultados); `:126-140` (grava o CPF e define `requer_revisao=False`, `pronto=True`); `nomear_lotes_por_cpf.py:80-91` (`chamar` sem paginação). Grava o `estado.json` direto (`:143-146`), sem a trava de `estado_store`.
- **Impacto:** para nomes comuns (MARIA, ANA, JOSÉ) há bem mais de 50 contatos e a regra "exatamente 1 casamento" é avaliada só na primeira página. Um homônimo exato fora dessa página não é visto, e um cliente novo que ainda não está no CRM recebe o CPF de outra pessoa com o mesmo nome. O lote vai para a fila sem revisão humana e gera contrato ou pasta com o CPF errado. Também há corrida com o ciclo, que pode desfazer ou corromper o estado.
- **Correção:** paginar (`start` até acabar) ou filtrar por `NAME`+`LAST_NAME` exatos; exigir um segundo fator (telefone ou data de nascimento) para auto-preencher; manter `requer_revisao=True` com o motivo `cpf_por_nome`; gravar via `estado_store.atualizar_estado`.
- **Confiança:** confirmado no código (falta de paginação e liberação automática). O limite de 50 por página é comportamento documentado da API REST do Bitrix.

### [S12] `certidoes.py`: webhook no argv do `curl` (visível no `ps`), parâmetros sem URL-encode e sem timeout
- **Severidade:** MÉDIA
- **Arquivo:linha:** `certidoes.py:100-108` (`args = ["curl", "-s", WEBHOOK + metodo + ".json"]` + `-d f"{k}={v}"`); chamadores com dado vindo de cliente ou IA: `:157` (`filter[%NAME]` = nome lido da certidão ou legenda) e `:302` (`MESSAGE` com nome e CPF).
- **Impacto:** (a) a URL com chave aparece em `/proc/<pid>/cmdline`, legível por qualquer usuário local durante a chamada; (b) `-d k=v` não codifica, então um `&`/`=`/`+` no nome ou na legenda divide ou injeta parâmetros (por exemplo, altera o `filter` do `crm.contact.list` e casa a certidão com outro contato, ou trunca o aviso); (c) sem `-m` nem `timeout=` no `subprocess.run`, um `curl` travado prende a rodada e o `flock` faz as seguintes "pularem" para sempre, sem alarme.
- **Correção:** trocar por `urllib.request` com `urllib.parse.urlencode(campos, doseq=True)` no corpo e `timeout=30` (como `nomear_lotes_por_cpf.chamar`); se ficar com o `curl`, usar `--data-urlencode`, `-m 30`, passar a URL via `-K -` (stdin) e `timeout=60` no `subprocess.run`.
- **Confiança:** confirmado no código.

### [S13] `certidoes.py` manda o X-Api-Key para qualquer URL devolvida pela API
- **Severidade:** MÉDIA
- **Arquivo:linha:** `certidoes.py:263-267` (`url = dados.get("downloadUrl") or dados.get("url")` e depois `urlopen(Request(url, headers={"X-Api-Key": CHAVE}))`).
- **Impacto:** se a API devolver URL assinada de CDN ou storage de terceiro (comum em download), a chave VibeCode vai junto para esse host. Para comparação, `download_svc.py:108` baixa a `DOWNLOAD_URL` sem header.
- **Correção:** só anexar o header se `urlparse(url).hostname == "vibecode.bitrix24.com"`.
- **Confiança:** confirmado no código. O host real da `downloadUrl` é **hipótese**.

---

## BAIXA

### [S14] Download inteiro em memória e sem teto de tamanho
- **Severidade:** BAIXA
- **Arquivo:linha:** `download_svc.py:108-117` + `_ler_resposta` (junta tudo em `bytes`); `telegram_grupo.py:136-137` (`fh.write(r.read())`).
- **Impacto:** um vídeo grande (S03/S10) pode estourar a memória do processo ou do WSL.
- **Correção:** streaming em chunks direto para `.part` (como `triagem_grupo.baixar_arquivo`) com teto (por exemplo, 50 MB) para tipos que não são documento.
- **Confiança:** confirmado no código.

### Itens verificados sem achado
- **TLS:** nenhum `verify=False`, `CERT_NONE`, `_create_unverified_context` ou `check_hostname` (grep vazio). O opener IPv4 do Telegram (`telegram_grupo.py:49-76`) mantém o `HTTPSConnection` padrão, com verificação.
- **Deserialização insegura:** sem `pickle`, `yaml.load`, `marshal`, `eval`, `exec`, `shell=True`, `os.system` ou `os.popen`. Todo `subprocess` usa lista de argumentos. Os `.sh` citam as variáveis (`"$BASE"`, `"$LIMITE"`); `PONTE_LIMITE` vem do ambiente da unit e é passado entre aspas.
- **Timeouts:** os `urlopen` têm timeout (`triagem_grupo` 10 s + teto de DNS via `_abrir_com_teto`, `download_svc` 30 s, `baixar_pendentes` 12 s, Telegram 90/120 s). A exceção é o S12. `lote_para_fila.py:367` e `certidoes.py:206` têm timeout só no segundo.
- **Token do Telegram em log:** `urllib` não inclui a URL na mensagem de `HTTPError`/`URLError`, então os `print` de erro (`telegram_grupo.py:272,426`) não vazam o token.

---

## Top 5 da frente (ordem de prioridade)
1. **S01:** parar de gravar a senha gov.br/Meu INSS em texto puro (estado, 1.833 backups, fila, `/tmp`) e sanear as cópias existentes.
2. **S03:** Telegram fail-closed: `chats_permitidos` vazio hoje = qualquer pessoa injeta documento na produção; preencher a allowlist ou desligar o canal.
3. **S05 + S04:** retenção dos backups do estado e da fila (27 GB com CPF) e tirar nome + CPF do `ponte.log`/journal, com logrotate.
4. **S06 + S07:** um único arquivo de segredos `0600`, sem varrer outros projetos nem `/tmp`; mascarar o webhook em `triagem_grupo.py:450,452`; rotacionar o webhook do usuário 1 e trocá-lo por um usuário técnico de escopo mínimo.
5. **S02 + S08/S09:** remover a credencial e os CPFs reais do código e dos testes; tirar os serviços do root, com `UMask=0077`, `PrivateTmp` e sem script em `/tmp`.

## O que NÃO deu para verificar sem o servidor
- Units do systemd (`User=`, `UMask=`, `PrivateTmp=`, destino do stdout) e se o `telegram_grupo.py --loop` está ativo.
- Permissões reais de `estado.json`, `*.bak-*`, `lotes/`, `staging/`, `vibecode.key`, `/root/.vibecode/api_key`, `/tmp/b24_webhook.txt` e do token do Telegram.
- Se o journal, o `ponte.log` ou o `triagem.log` já contêm a URL do webhook com chave (S07) e quantos lotes têm senha gov.br no texto (S01).
- Se o webhook `rest/1/` é de usuário administrador e quais escopos ele tem; se o `bitrix24_mcp_server.py` e o PrevSync-CRM têm a chave em texto.
- O `fs.protected_regular` do kernel e se há outros usuários locais (exploração de S06(c)/S09).
- O comportamento do worker/painel com arquivo de extensão arbitrária no staging (S10).
