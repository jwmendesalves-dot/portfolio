# Entrega da remediação — triagem Bitrix

Data: 09/11/2026 · branch `remediacao` · base auditada: `fbc440c`.

Resumo: a auditoria encontrou **101 problemas**. Hoje **86 estão corrigidos e testados**, **11 estão
parcialmente resolvidos** (o código está pronto, mas falta executar algo no servidor ou falta uma
decisão sua) e **4 eram só documentação**. Durante o trabalho apareceram **28 problemas novos**, quase
todos já corrigidos. **O sistema novo ainda não pode ir para o servidor**: há três bloqueios, listados abaixo.

## 1. O que foi auditado

Todo o código da triagem que roda em `/opt/triagem-bitrix`:

- leitura do grupo de documentos no Bitrix e montagem dos lotes (kits por cliente);
- identificação do cliente (CPF, nome, telefone);
- download dos arquivos;
- ponte para a fila de jobs (envio ao painel);
- canal antigo do Telegram;
- certidões de nascimento e pintura da planilha;
- segurança (senhas, chaves, dados pessoais em logs e backups);
- serviços do servidor (systemd), logs e backups;
- a suíte de testes.

Também foi feita uma coleta real do servidor em 09/11 (`SERVIDOR-COLETA-20261109.md`).

## 2. O que mudou, por área

**Leitura do grupo e lotes.** O sistema relia e reagrupava as mesmas mensagens a cada ciclo e criava
lotes "pedaço" novos toda vez (num teste com 3 kits, saíam 19 lotes). Agora cada chat tem um marcador
de onde parou, o lote tem identificador estável e 3 kits viram 3 lotes. Um ciclo não apaga mais o que
outro programa gravou no mesmo lote. Se o arquivo de estado estiver corrompido, o ciclo para em vez de
gravar por cima.

**Identificação / CPF.** CPF agora sempre passa pela checagem do dígito verificador. "Bom dia" ou
"segue documento" não são mais tratados como nome de cliente. O preenchimento de CPF pelo nome só
confirma com uma segunda prova (telefone do mesmo contato); com nome igual de outra pessoa, vira só
sugestão para revisão. Nada disso libera mais o lote para envio sozinho.

**Download.** Fila própria com tentativas, espera crescente (de minutos a horas) e separação entre erro
passageiro e erro definitivo. Arquivo grava primeiro em temporário, confere tamanho e sha256 e só
depois vai para o lugar. Áudio e vídeo são registrados mas não baixados. Link expirado é renovado.
Arquivos com o mesmo nome no mesmo lote não se sobrescrevem mais.

**Ponte / fila.** Uma única regra decide o que pode ir para a fila. Kit com arquivo faltando não vai
pela metade. Duplicata com download incompleto vai para revisão, não é descartada. Duas execuções ao
mesmo tempo não perdem registro (trava compartilhada). Falha no meio não deixa sobra nem job errado.
O monitor só avisa quando algo muda.

**Telegram (canal antigo).** Lista de chats vazia não libera mais qualquer pessoa. O código passou a ler
a lista no lugar certo do config. O "Recebido" só é enviado depois de gravar. Arquivo truncado não é
aceito; acima de 20 MB vira erro definitivo sem tentar baixar.

**Certidões e planilha.** Falha não marca mais a mensagem como processada. Foto de certidão é lida
como imagem. O cliente é achado pela mãe/responsável, nunca pelo nome da criança. Certidão de casamento
e óbito ficam de fora. A pintura relê a linha antes de gravar (não sobrescreve cliente vizinho), não abre
a planilha à toa e não cria linha com CPF inválido ou cliente incerto.

**Segurança / segredos.** O webhook não é mais "procurado" em arquivos do disco e não aparece em log.
CPF, chaves e tokens saem mascarados nos logs. A senha gov.br do cliente não é mais gravada no estado.
Credencial e CPFs reais foram retirados do código e dos testes. Arquivos temporários são privados.
Novo modo estrito de segredos (recusa arquivo com permissão aberta).

**Deploy.** Novas units do systemd, logrotate, scripts de alerta e de poda de backup, todos no repo
(`deploy/`). O passo da fila virou um único script com trava compartilhada com a ponte. Plano de
implantação com checkpoints e rollback.

**JEV (TypeSafe).** Novo decisor que pode consultar a IA em três pontos (documento, foto, lote). Vem
**desligado**. Mesmo ligado, nunca promove um dono de documento desconhecido e nunca decide sozinho
com confiança baixa ou inválida.

## 3. Mudanças de comportamento que você vai notar

1. **Kit sem traço espera 30 min** antes de ser gravado. Antes era gravado já no ciclo seguinte, às vezes
   incompleto.
2. **Config inválido para o programa com código 3** e mensagem clara, em vez de rodar silenciosamente
   com padrões errados. No modo contínuo, ele espera e tenta de novo.
3. **CPF sem dígito verificador correto vai para revisão** (com motivo), não é aceito nem descartado.
4. **O ciclo volta a baixar arquivos.** Esse caminho estava parado desde 27/10 por um defeito; quem baixava
   era só o timer de 10 min. Vale observar a duração do ciclo nas primeiras horas.
5. **Kit com áudio vai para a fila.** Antes ficava preso esperando download para sempre.
6. **Erro 401 ou OVERLOAD do Bitrix não marca o arquivo como perdido**; só "acesso negado" ao arquivo é
   definitivo.
7. **Cache de telefone antigo é descartado** na primeira execução (o formato antigo podia dar a prova de
   uma pessoa para um homônimo).
8. **Nome por CPF só com segunda prova**: alguns lotes que antes "resolviam sozinhos" vão aparecer em revisão.
9. **Modo estrito de segredos** (`TRIAGEM_SEGREDOS_ESTRITO=1`) recusa segredo com permissão aberta. Vem
   desligado; ligar só depois do `chmod 600`.
10. **JEV desligado por padrão.** Nada chama a IA até você ligar.
11. **Os serviços devolvem o código de erro real ao systemd** e disparam alerta; antes escondiam falhas.
12. **Monitor mais quieto**: só avisa quando a situação muda.
13. **Job no painel sem senha em texto**: aparece `S/SENHA` + aviso (decisão final sua, ver item 5.4).

## 4. Bloqueios antes do deploy

1. **C0 — OCR e TypeSafe do servidor.** O servidor tem código que não está no repo
   (`ocr_documento.py`, `typesafe.py`, `typesafe_smoke.py`, uma versão de `lote_para_fila.py` e chaves do
   `config.json`). Copiar o repo por cima apagaria isso. É preciso trazer esses arquivos, integrar e testar
   antes. Passo a passo no PLANO-IMPLANTACAO (C0).
2. **Coletar duas units que não foram vistas:** `triagem-bitrix-bot.service` (o bot contínuo) e
   `triagem-bitrix.service` (confirmar se existe). Sem isso, a troca das units pode quebrar o bot ou deixar
   um escritor do estado de fora.
3. **`flock` e `timeout` no servidor.** Os scripts novos param com erro se faltarem (checkpoint C1).

## 5. Ações que dependem de você

1. **Trocar a chave TypeSafe** que ficou exposta.
2. **Rotacionar webhook do Bitrix, chave VibeCode e token do Telegram.** Saíram do código, mas continuam
   no histórico do git. O mesmo vale para a senha de cliente e os CPFs que estavam no código.
3. **Decidir o descarte dos 1.833 backups antigos (27 GB)** com CPF, senha e mensagens. A ferramenta está
   pronta (`deploy/bin/podar-backups.py`, `manutencao/sanear_senhas.py`, ambas só simulam por padrão);
   recomendável ouvir o jurídico.
4. **Decidir a senha no job** (S01): o painel ainda usa a senha; hoje o job sai com `S/SENHA`.
5. **Decidir a regra "CPF válido + nome curto"** (ex.: cabeçalho `= JOAO` com CPF válido). Hoje não conta
   como fechamento pelo traço; há um teste marcado como pendência esperando essa decisão.
6. **Ligar `TRIAGEM_SEGREDOS_ESTRITO=1`** depois de `chmod 600` em todos os arquivos de segredo.
7. **Calibrar o JEV em modo sombra** antes de deixar decidir: ele consulta, grava o resultado e continua
   usando a regra atual. Ligar "decide" só com taxa de concordância medida.

## 6. Números

- Testes (rodado agora, `.venv/bin/python -m pytest -q`):

  ```
  1332 passed, 2 skipped, 1 xfailed in 21.78s
  ```

  O `xfailed` é a regra "CPF válido + nome curto" (item 5.5). A rodada foi feita com ajustes de
  testes da frente R4 (revisão final foco 4) ainda não commitados na árvore; rodar de novo depois do commit.
- Commits desde `fbc440c`: **62** (57 sem contar merges) — `git log --oneline fbc440c..HEAD | wc -l`.
- Achados: 101 auditados → 86 corrigidos, 11 parciais, 4 documentados; 28 novos (18 corrigidos com
  teste, 8 ajustes de deploy sem teste automático, 2 parciais).

## 7. Onde está cada documento (`docs/remediacao/`)

| Documento | Para que serve |
|---|---|
| `ENTREGA.md` | este resumo |
| `MATRIZ-ACHADOS.md` | os 101 achados + 28 novos, um por linha, com status, commit e teste |
| `PLANO-IMPLANTACAO.md` | passo a passo do deploy, checkpoints C0–C10 e rollback |
| `RUNBOOK-OPERACIONAL.md` | operação do dia a dia, alertas, o que fazer em cada falha |
| `CONTRATOS.md` | regras entre os módulos (quem grava o quê no estado) |
| `REGRAS.md`, `ARQUITETURA-CORRIGIDA.md` | regras de negócio e desenho atual |
| `PLANO-RECUPERACAO-DADOS.md` | recuperação de dados históricos (R0–R6) |
| `SERVIDOR-COLETA-20261109.md` | o que foi coletado no servidor real e as diferenças com o repo |
| `frentes/*.md` | relatório de cada frente de correção |
| `revisao/*.md` | revisões independentes (onda 1 e revisão final em 4 focos) |
| `deploy/` (raiz do repo) | units, logrotate, polkit e scripts para o servidor |
