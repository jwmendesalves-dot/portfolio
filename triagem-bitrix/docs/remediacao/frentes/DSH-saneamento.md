# Frente DSH — saneamento do histórico de senhas (S01/S05 retro)

Status: **concluído** (ferramenta criada e testada com dados sintéticos; a execução no
servidor é posterior, com autorização, e começa pelo inventário).

Escopo: a **limpeza retroativa** do S01 (senha gov.br/Meu INSS já gravada em texto puro
no histórico) + o lado de saneamento do S05. O código novo (C1) já não grava a senha;
esta frente entrega a ferramenta que remove o que ficou. **Nada é apagado**: a ferramenta
só reescreve conteúdo saneado; a poda/descarte continua sendo outra ferramenta.

## Arquivos
- `manutencao/sanear_senhas.py` (novo) — a ferramenta (CLI, stdlib + módulos do repo, Python 3.9).
- `testes/test_sanear_senhas.py` (novo) — 15 testes, tudo sintético.
- `docs/remediacao/frentes/DSH-saneamento.md` (este relatório).

## O que a ferramenta faz

`manutencao/sanear_senhas.py` inventaria e saneia a senha gov.br/Meu INSS do histórico.
Detector: `seguranca.remover_senhas(texto) != texto` (o mesmo padrão do grupo, da
fichinha e da tabela do contrato — ver `testes/test_seguranca.py`). O valor da senha vira
`[senha removida]`; nome, CPF, telefones e o resto do texto ficam intactos (é para a
triagem ler).

**Alvos** (padrões via `caminhos.em_base`/`caminhos.em_separador`; argumento explícito
substitui o padrão):
| flag | alvo | padrão |
|---|---|---|
| `--estado ARQ` | `estado.json` | `$ESTADO_JSON` ou `caminhos.em_base("estado.json")` |
| `--backups-dir DIR` (repetível) | `*.bak-*` e `fila-jobs-*.json` do diretório | `em_base("backups-estado")`, `em_base("backups")` (backups da fila) e o diretório do estado (os `estado.json.bak-*` legados) |
| `--fila-jobs ARQ` | `fila-jobs.json` do painel | `caminhos.em_separador("data", "fila-jobs.json")` |
| `--extra ARQ` (repetível) | qualquer JSON (ex.: `/tmp/_entradas.json`) | — |

**Modos** (o padrão é o inventário, somente leitura):
- `--inventario` (padrão): percorre os alvos e imprime **só contagens por arquivo** —
  registros com senha, mensagens/campos com senha, tamanho, permissão (octal) e dono.
  Nunca imprime trecho de texto, nome, CPF ou senha (os testes asseguram isso também no
  `--json`). Nenhum byte/mtime/permissão é alterado.
- `--aplicar` **sem** `--confirmar`: simulação — mostra o que seria feito, grava nada.
- `--aplicar --confirmar` (os dois obrigatórios): escreve.
  - `estado.json` → `estado_store.atualizar_estado` (flock + gravação atômica; sem
    mudança o store não regrava), aplicando `seguranca.remover_senhas` em **todo campo
    de texto de `lotes[*].mensagens[*]`** — a estrutura real gravada por
    `triagem_grupo.serializar_lote` (`mensagens[].{id,autor,data,sinal,texto,realocada}`).
  - demais JSONs → cópia saneada atômica (tmp exclusivo 0600 + fsync + `os.replace`),
    preservando a estrutura JSON; arquivos reescritos ficam **0600**.
  - backups (`--sanear-backups --ciente-evidencia`): cada backup é reescrito saneado
    **mantendo nome e mtime**; nada é apagado/renomeado.
  - fila do painel (`--incluir-fila-painel`): sob `estado_store.trava_arquivo` (flock em
    `<arq>.lock`). O painel (Node) consome esse arquivo ao vivo e a trava dele não está
    nesta cópia do agente-separador: **compatibilidade = conferir no servidor** (ou rodar
    com o painel/worker parados).

**Códigos de saída:** `0` ok (inventário limpo ou aplicação concluída); `1` a varredura
encontrou senha e nada foi gravado (inventário/simulação); `2` erro de leitura/JSON
inválido em algum alvo, ou recusa de segurança (`--sanear-backups` sem
`--ciente-evidencia`, `--confirmar` sem `--aplicar`). Arquivo JSON inválido: pulado com
aviso em stderr, sem parar os demais; o `2` vem no fim.

**Idempotente:** a 2ª execução não muda nada (bytes/mtime) e reporta `0`.

**NÃO faz (de propósito):** não apaga nada (a poda é `estado_store.podar_backups` e
`lote_para_fila.podar_backups_fila`), não cria novo backup (um backup novo replicaria a
senha — ver riscos), não mexe em CPF/nome, não usa rede, não lê nem grava fora dos alvos.

## Como rodar no servidor (passo a passo, começando pelo inventário)

0. **Antes de tudo:** janela de manutenção com o ciclo da triagem e a ponte parados (ou
   aceite a corrida; a ferramenta usa a trava do `estado_store` no estado). Guarde a
   versão de cada ferramenta/commit e a data. Decida com o dono se a **evidência do
   incidente LGPD** (S01) precisa ser preservada ANTES de qualquer reescrita
   (imagem/hash dos arquivos com senha, fora do disco de dados, para o RIPD/ANPD).

1. **Inventário (somente leitura):**
   ```sh
   python3 manutencao/sanear_senhas.py --inventario | tee /root/s01-inventario-$(date +%F).txt
   python3 manutencao/sanear_senhas.py --inventario --json > /root/s01-inventario-$(date +%F).json
   ```
   Sem flags, ele já varre: `estado.json`, `backups-estado/`, `backups/` (fila) e os
   `estado.json.bak-*` legados ao lado do estado, e a `fila-jobs.json` do painel. Para
   incluir o `/tmp/_entradas.json` histórico: `--extra /tmp/_entradas.json`. Confira os
   totais, a permissão e o dono de cada arquivo (entrada para o S08). Saída `1` = há
   senha (esperado nesta fase).

2. **Preservar evidência (se aplicável):** `sha256sum` dos arquivos listados com senha,
   gravado e guardado fora do disco de dados. Registrar a decisão no RIPD.

3. **Simulação:** `python3 manutencao/sanear_senhas.py --aplicar` (com os mesmos alvos).
   Confira o que seria saneado vs. o que ficaria `pulado`.

4. **Saneamento do vivo (estado + fila + extras):**
   ```sh
   python3 manutencao/sanear_senhas.py --aplicar --confirmar --incluir-fila-painel
   ```
   Para a `fila-jobs.json` do painel: **conferir no servidor** se o `FilaJobs` do painel
   usa a mesma trava (`fila-jobs.json.lock`); se não usar, rode com o painel parado.

5. **Backups (só depois da evidência preservada):**
   ```sh
   python3 manutencao/sanear_senhas.py --aplicar --confirmar --sanear-backups --ciente-evidencia
   ```
   Reescreve cada backup saneado, mantendo nome e mtime. O aviso LGPD é impresso sempre
   que `--sanear-backups` aparece; sem `--ciente-evidencia` a escrita inteira é recusada.

6. **Fila do painel (só com autorização explícita):** acrescente `--incluir-fila-painel`.

7. **Conferência:** repetir o passo 1 — tem que sair `com_senha=0 ... codigo=0`. Repetir
   o passo 4/5 para provar a idempotência (`alteracoes=0`).

8. **Descarte (OUTRA ferramenta, decisão do dono):** só depois do saneamento e da
   preservação de evidência, `estado_store.podar_backups(..., simular=False)` e
   `lote_para_fila.podar_backups_fila(...)` com a política de retenção aprovada.

## Riscos
- **Evidência de incidente LGPD:** reescrever os backups destrói a prova original do
  vazamento (S01). Se houver (ou puder haver) investigação/RIPD/ANPD, preservar
  cópia+hash ANTES (por isso `--ciente-evidencia` é obrigatório).
- **Corrida com processos vivos:** a ferramenta trava o `estado.json` (contrato do
  `estado_store`); para os demais JSONs a gravação é atômica, mas não há trava do outro
  lado para a `fila-jobs.json` do painel (trava compatível = **conferir no servidor**).
- **Escopo do estado:** só `lotes[*].mensagens[*]` é saneado no `estado.json` (é onde o
  S01 gravava). Se o inventário apontar senha em OUTRO campo do estado, a ferramenta
  reporta no alvo correspondente apenas o que ela consegue sanear — caso conhecido hoje:
  nenhum. Nos demais JSONs (fila/backups) o saneamento alcança toda string.
- **Painel exige a senha na fichinha (frente J):** jobs NOVOS continuam podendo levar a
  senha na `fichinha` (decisão pendente do dono, J-job-ficha.md). Saneamos o HISTÓRICO;
  se a ponte voltar a enfileirar com senha, o histórico reacumula — a retenção (S05) e a
  decisão (a)–(d) da frente J são o fechamento disso.
- **Não cria backup novo de propósito:** `fazer_backup` antes de sanear replicaria a
  senha; quem quiser snapshot pré-saneamento deve gerá-lo e saneá-lo também (ou guardar
  como evidência controlada).
- **JSON com formato não canônico** (indentado etc.) é regravado em formato compacto na
  primeira escrita; a estrutura de dados é preservada.

## Evidências

### Testes (reais, nesta máquina, dados sintéticos — CPFs de `cpf_util.gerar_para_teste`)
```
$ .venv/bin/python -m pytest -q testes/test_sanear_senhas.py
...............                                                          [100%]
15 passed in 0.15s

$ .venv/bin/python -m pyflakes manutencao/sanear_senhas.py
(saída vazia; exit 0)

$ .venv/bin/python -m pytest -q
...
1 failed, 963 passed, 2 skipped in 14.79s
```
A única falha é **pré-existente e não desta frente**: 
`testes/test_seguranca.py::test_legado_em_tmp_recusado[/var/tmp]` — o teste cria um
arquivo em `/var/tmp` e este ambiente bloqueia a escrita lá (`PermissionError` no
`tempfile.mkstemp`); a baseline antes de qualquer edição já falhava igual
(`1 failed, 948 passed, 2 skipped`). Os 963 = 948 + 15 novos.

O que os 15 testes cobrem: inventário conta por arquivo e não vaza senha/CPF/nome (texto
e `--json`); inventário não escreve nada (bytes/mtime/modo idênticos); `--aplicar` sem
`--confirmar` não grava; `--aplicar --confirmar` saneia o estado e preserva todos os
demais campos do lote/mensagem; 2ª execução é no-op (`alteracoes=0`, bytes/mtime iguais);
JSON inválido → código 2 sem afetar os outros alvos (que são saneados); arquivo
inexistente explícito → código 2; backups exigem `--ciente-evidencia` (recusa total sem
ela, com o aviso citando `podar_backups`/ANPD) e, saneados, mantêm nome e mtime e ficam
0600; todos os arquivos reescritos ficam 0600; fila do painel só com `--incluir-fila-painel`
e sob a trava do `estado_store` (`.lock` criado); alvos padrão vêm de
`caminhos.em_base/em_separador`.

### Demonstração da CLI (fixtures sintéticas em diretório temporário)
```
$ .venv/bin/python manutencao/sanear_senhas.py --inventario <alvos>
/tmp/sanear-demo-tuhr/estado.json  papel=estado  registros=2  registros_com_senha=2  itens_com_senha=3  tamanho_bytes=2725  permissao=0600  acao=inventariado  dono=usuario(502)
/tmp/sanear-demo-tuhr/fila-jobs.json  papel=fila-painel  registros=2  registros_com_senha=1  itens_com_senha=1  tamanho_bytes=326  permissao=0644  acao=inventariado  dono=usuario(502)
/tmp/sanear-demo-tuhr/backups-estado/estado.json.bak-20990101-000000  papel=backup  registros=2  registros_com_senha=2  itens_com_senha=3  tamanho_bytes=2725  permissao=0644  acao=inventariado  dono=usuario(502)
/tmp/sanear-demo-tuhr/backups-estado/fila-jobs-20990101-000000.json  papel=backup  registros=2  registros_com_senha=1  itens_com_senha=1  tamanho_bytes=326  permissao=0644  acao=inventariado  dono=usuario(502)
/tmp/sanear-demo-tuhr/entradas.json  papel=extra  registros=2  registros_com_senha=1  itens_com_senha=1  tamanho_bytes=326  permissao=0644  acao=inventariado  dono=usuario(502)
TOTAL modo=inventario arquivos=5 com_senha=5 itens_com_senha=9 alteracoes=0 erros=0 codigo=1
exit=1

$ .venv/bin/python manutencao/sanear_senhas.py --aplicar --confirmar --sanear-backups <alvos>
... (aviso LGPD completo em stderr)
RECUSADO: --sanear-backups em modo de escrita exige --ciente-evidencia ... NADA foi gravado.
exit=2

$ .venv/bin/python manutencao/sanear_senhas.py --aplicar --confirmar --sanear-backups --ciente-evidencia --incluir-fila-painel <alvos>
... (5 linhas com acao=saneado)
TOTAL modo=escrita arquivos=5 com_senha=5 itens_com_senha=9 alteracoes=5 erros=0 codigo=0
exit=0

$ .venv/bin/python manutencao/sanear_senhas.py --aplicar --confirmar --sanear-backups --ciente-evidencia --incluir-fila-painel <alvos>
... (5 linhas com acao=sem_alteracao, registros_com_senha=0, permissao=0600)
TOTAL modo=escrita arquivos=5 com_senha=0 itens_com_senha=0 alteracoes=0 erros=0 codigo=0
exit=0

$ .venv/bin/python manutencao/sanear_senhas.py --inventario <alvos> | tail -1
TOTAL modo=inventario arquivos=5 com_senha=0 itens_com_senha=0 alteracoes=0 erros=0 codigo=0
```
Nenhum trecho de texto, nome, CPF ou senha aparece em qualquer saída (os testes
`test_inventario_*` afirmam isso byte a byte sobre stdout+stderr).

## Pendências para outras frentes / orquestrador
1. **Execução no servidor** (com autorização, ordem acima): inventário → evidência →
   aplicação → backups → conferência. Só o servidor tem os ~1.833 `estado.json.bak-*`
   (27 GB), os `backups/fila-jobs-*` (737 MB) e o `/tmp/_entradas.json`.
2. **Conferir no servidor:** a trava do `FilaJobs` do painel (Node) sobre
   `/opt/agente-separador/data/fila-jobs.json` — se não for o mesmo `flock` em
   `fila-jobs.json.lock`, rodar com o painel parado.
3. **Frente J (decisão do dono):** a `fichinha` de jobs novos ainda pode levar a senha;
   enquanto isso, o histórico reacumula em `fila-jobs.json`/`backups/` (o saneamento é
   periódico ou a decisão (a)–(d) fecha isso).
4. **Frente E/S05:** política de retenção/poda (`podar_backups`, `podar_backups_fila`)
   depois do saneamento e da preservação de evidência; a ferramenta deliberadamente não
   apaga nada.
5. **S08 (permissões):** o inventário já mostra permissão e dono de cada arquivo com
   senha — insumo direto para o `chmod`/`UMask` da frente de endurecimento.
