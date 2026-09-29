# Discord-TTS

[English](README.md) · **Português**

Bot de Discord que entra num canal de voz e fala um texto com a voz de quem
você escolher, usando [Pocket TTS](https://github.com/kyutai-labs/pocket-tts)
rodando na CPU. Vem com um painel web local para ligar e desligar tudo, gerar
áudio, clonar vozes novas e ver as estatísticas de uso.

Sem framework web, sem banco, sem build. O bot não usa FFmpeg; as ferramentas
de preparação usam.

```
bot.py, tts_engine.py, audio_source.py   o bot
engine.py, engine_api.py                  o processo pesado: modelo + API local
panel/, Panel.pyw, Panel.bat              o painel web e o lançador dele
voice.py                                  clonar vozes e gerar áudio (CLI)
preparation.py                            áudio bruto -> amostra de referência
history.py                                histórico de uso + relatórios
tools/                                    preparação em lote e diagnóstico
```

Desenvolvido e testado no Windows 11. O lançador do painel e o ícone na bandeja
foram pensados para o Windows; o bot e o motor são Python comum.

O código, os comandos e a interface estão em inglês. Este README é a versão em
português da documentação.

## Antes de tudo: consentimento

Clonar a voz de outra pessoa exige autorização explícita dela. A licença do
Pocket TTS proíbe expressamente o uso sem consentimento, e no Brasil voz é dado
pessoal protegido pela LGPD. Peça antes de gravar, não depois.

## Setup

Em resumo — os detalhes de cada passo vêm logo abaixo:

| Passo | Serve para |
|---|---|
| 1. `pip install -r requirements.txt` | tudo |
| 2. Token do Hugging Face (aceitar os termos do Pocket TTS) | baixar o modelo no primeiro start |
| 3. Token do bot do Discord, bot convidado para o servidor | falar no Discord (não para gerar arquivos de áudio) |
| 4. `.env` com os dois tokens | tudo |
| 5. FFmpeg no PATH | clonar vozes, baixar `.ogg` |
| 6. Um `.venv` com Python 3.11 e o `requirements-prep.txt` | clonar vozes |
| 7. Pelo menos uma voz em `voices/` | falar qualquer coisa |

Se faltar o passo 5 ou 6, o painel mostra um aviso no topo com os comandos
exatos para rodar; ele some sozinho quando tudo estiver no lugar.

**1. Dependências**

São dois ambientes Python separados, e a separação é obrigatória — não tente
juntar. O segundo só é necessário para clonar vozes.

*Bot, motor, painel e `voice.py`* — o Python do sistema (3.10+; testado no 3.14):

```powershell
pip install -r requirements.txt
```

*`preparation.py`* — um venv com **Python 3.11**, em `.venv` na raiz do projeto
(é lá que o painel procura):

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements-prep.txt --extra-index-url https://download.pytorch.org/whl/cpu
```

Tem que ser 3.11: o `deepfilterlib` só publica wheel `cp311`. E as versões do
`requirements-prep.txt` estão pinadas por motivo:

| Pacote | Versão | Por quê |
|---|---|---|
| torch / torchaudio | 2.1.2+cpu | 2.2+ removeu `torchaudio.backend.common`, que o `df/io.py` do DeepFilterNet importa |
| setuptools | <81 | 81+ removeu `pkg_resources`, que o resemblyzer importa |
| numpy | <2 | exigência do torch 2.1.2 |

Se atualizar sem testar, quebra.

Também é preciso o [FFmpeg](https://ffmpeg.org) no PATH para preparar amostras e
baixar `.ogg` (`winget install Gyan.FFmpeg` no Windows).

**2. Acesso ao modelo**

Os pesos do Pocket TTS são distribuídos com um acordo de uso. Faça login no
Hugging Face, aceite o acordo em https://huggingface.co/kyutai/pocket-tts e gere
um token em https://huggingface.co/settings/tokens.

**3. Bot do Discord**

Em https://discord.com/developers/applications: crie a aplicação, vá em **Bot**,
gere o token. Em **OAuth2 → URL Generator** marque os escopos `bot` e
`applications.commands`, e as permissões *Connect* e *Speak*. Use a URL gerada
para adicionar o bot ao servidor.

Não é necessário ativar nenhum *privileged intent* — o bot só usa slash commands.

**4. Configuração**

```powershell
copy .env.example .env
```

Preencha `DISCORD_TOKEN` e `HF_TOKEN`; o resto está comentado no arquivo. Para
falar em português, use `TTS_LANGUAGE=portuguese_24l` (ver [Idiomas](#idiomas)).

**5. Vozes**

Uma voz é clonada a partir de 15–30 segundos de fala limpa (ver
[Qualidade das amostras](#qualidade-das-amostras)). O jeito mais fácil é a aba
**Clone** do painel, que prepara o áudio, deixa você ouvir o resultado e só
então salva. Pela linha de comando, prepare a amostra antes
([Preparar uma amostra de referência](#preparar-uma-amostra-de-referência)) e
depois:

```bash
python voice.py clone caminho/amostra_ref.wav alice
```

Gera `voices/alice.safetensors` e um áudio de conferência em `outputs/`. O bot
enxerga a voz nova na hora, sem reiniciar — ele faz glob em `voices/` a cada
comando.

**6. Rodar**

Dois cliques em `Panel.bat`. Ele abre o painel no navegador e põe um ícone na
bandeja, ao lado do relógio. Não abre janela de terminal.

O painel tem dois interruptores separados:

- **Engine** (motor) — carrega o modelo e permite gerar áudio. O primeiro start
  baixa os pesos e demora; os seguintes usam o cache.
- **Discord** — conecta o bot ao servidor. Ligar isto liga o motor junto.

A separação existe porque gerar um áudio para o WhatsApp não deveria exigir
aparecer online para os seus amigos. Os dois sempre nascem desligados.

### Generate (gerar)

Escolha a voz, escreva o texto, ouça no navegador. Exige o motor ligado; não
exige o Discord. Os arquivos ficam em `outputs/`, a mesma pasta do
`python voice.py say` — e o botão de `.ogg` usa os mesmos parâmetros do
WhatsApp (Opus mono 32 kbps, perfil VoIP).

Gerar pelo painel entra na **mesma fila** que falar no Discord: se o bot está no
meio de uma frase, o teste espera a vez em vez de disputar o modelo com ela.

### Clone (clonar)

Quatro passos: o áudio vai para `work/pending/`, o `preparation.py` roda no
Python 3.11 do `.venv` com o diagnóstico ao vivo na tela, você dá o nome e ouve
a frase de teste, e só então **Save** move a voz para `voices/`.

Uma voz com nome repetido pede confirmação. É a diferença em relação ao
`python voice.py clone`, que escreve direto e avisa "overwriting" depois do
fato — aqui, uma tentativa ruim não destrói uma voz que já funcionava.

O áudio enviado e o `_ref.wav` ficam em `work/pending/` depois de salvar, de
propósito: são o material de referência, e servem para tentar de novo com outro
nome. Limpar a pasta é seguro a qualquer momento.

### Config

As chaves editáveis do `.env`, uma por campo. As linhas do `DISCORD_TOKEN` e do
`HF_TOKEN` não aparecem porque o painel não sabe que elas existem: ele lê e
reescreve apenas as chaves da lista branca, linha por linha, e devolve todo o
resto byte a byte. Um `.env.bak` é gravado antes de cada escrita.

O bot lê as chaves uma vez, quando o motor sobe, então mudar alguma do lado do
Discord pede o botão **Restart the engine** — que leva os mesmos segundos do
start normal.

### Quando o motor cai sozinho

O painel percebe, avisa no log com o código de saída e religa **uma vez**. Se
cair de novo, ele para e diz que parou: se o token está errado ou os pesos não
baixam, religar cinquenta vezes não conserta. Clicar em **Start** à mão rearma a
tentativa automática.

Se preferir sem painel, o motor roda sozinho:

```bash
python engine.py
```

Ele sobe uma API em `127.0.0.1:8081`, mas não conecta ao Discord por conta
própria — quem manda conectar é o painel.

## Uso no dia a dia

```bash
python voice.py list                          # vozes disponíveis (instantâneo)
python voice.py clone amostra.wav alice       # WAV de referência -> voz nova
python voice.py say alice "texto"             # -> outputs/alice_texto.wav
python voice.py say alice "texto" --whatsapp  # -> .ogg de mensagem de voz
```

`--whatsapp` entrega Opus mono 32 kbps com perfil de voz, o mesmo formato do
áudio do WhatsApp. `--play` abre o arquivo ao terminar; `-o` escolhe o caminho
na mão.

O `say` passa pelo mesmo `TTSEngine.stream()` que o bot usa e imprime o fator
de tempo real. Se soar bem aqui, soa bem no Discord.

## Preparar uma amostra de referência

Roda no venv 3.11. **Ative antes** — fora dele o import do DeepFilterNet falha:

```powershell
.\.venv\Scripts\Activate.ps1
python .\preparation.py '.\samples\PESSOA\audio.ogg'
deactivate
```

Sai `audio_ref.wav` na mesma pasta do arquivo de entrada. Esse `_ref.wav` é o
que você passa para o `voice.py clone`.

Aceita `.opus .ogg .oga .m4a .mp3 .wav .flac .aac .webm .mp4` — não precisa
converter nada antes, o ffmpeg lê tudo. Aceita também uma **pasta**, e aí
processa todos os áudios de dentro, pulando os `_ref` que já existem.

| Flag | O que faz |
|---|---|
| `--sr N` | sample rate de saída (padrão 24000, o nativo do Pocket TTS) |
| `--lufs N` | loudness alvo (padrão −23) |
| `--trim` | corta silêncios longos |
| `--check` | similaridade de locutor via resemblyzer |
| `--enhance` | variante resemble-enhance — exige `pip install resemble-enhance` (o `deepspeed` não compila no Windows) |

Pipeline: ffmpeg (mono 48k, sem processar) → DeepFilterNet3 → loudnorm −23 LUFS
→ resample.

**Deliberadamente sem gate, compressor ou EQ.** O speaker encoder codifica o
processamento junto com a voz — se você comprimir a referência, o clone sai com
a compressão embutida.

O `--check` mede distância de embedding, **não** qualidade do clone. Serve para
triagem em lote. Rodar sobre uma saída já processada dá 0.999 e não quer dizer
nada.

### Gravando pelo Craig

No [Craig](https://craig.chat), baixe **Multi-track → FLAC**: uma faixa por
pessoa, sem perda. Evite AAC e Ogg Vorbis, que recodificam material já lossy.

As faixas vêm com a duração inteira da call. Corte um trecho bom antes de
processar — `-c copy` fatia sem recodificar:

```powershell
ffmpeg -i faixa.flac -ss 00:04:30 -t 25 -c copy recorte.flac
```

### Ferramentas

```bash
python tools/whatsapp_samples.py exports --me "Seu Nome"
```

Recebe um `.zip` de exportação de conversa do WhatsApp (ou uma pasta com
vários), separa os áudios por remetente lendo o `_chat.txt`, converte para WAV e
ranqueia por qualidade, deixando os melhores em `work/NOME/candidates/`. Você
ouve e clona o melhor com o `voice.py`.

| Ferramenta | O que faz |
|---|---|
| `tools/rank_samples.py PASTA` | pontua uma pasta de WAVs e ranqueia os melhores candidatos a amostra |
| `tools/diagnose.py resampler` | verifica cliques nas junções de chunk sem carregar o modelo |
| `tools/diagnose.py smoke` | valida a instalação e o download com uma voz pronta do catálogo |
| `tools/spectrogram.py A B` | espectrograma lado a lado de dois arquivos (rode no `.venv`) |

## Qualidade das amostras

O modelo reproduz a qualidade do áudio de referência, não só o timbre. Amostra
com chiado gera voz com chiado. Vale mais 20 segundos limpos do que 3 minutos
recortados de gravação de call.

- 15–30 segundos de fala contínua, sem pausas longas
- Ambiente sem eco, sem música, sem outras pessoas falando por cima
- No registro que você quer reproduzir (gravou sussurrando, o clone sussurra)
- Passe por um *enhancer* (ex.: Adobe Podcast Enhance) antes de importar

Se você tem gravação de call com todo mundo misturado, o caminho mais barato não
é diarização — é pedir para cada um gravar 20 segundos no celular. A
alternativa é um bot de gravação multi-track, que já entrega uma faixa por
pessoa.

### A fonte é o teto (medido)

Áudio do WhatsApp é Opus em banda estreita. Medindo um clipe real:

| | 99% da energia | banda 4–8k | banda 8–12k |
|---|---|---|---|
| `.ogg` cru do WhatsApp | 4.2 kHz | −19.5 dB | −35.6 dB |
| depois do `preparation.py` | 4.5 kHz | −18.5 dB | −34.2 dB |
| clone gerado do `_ref.wav` | 4.0 kHz | −20.1 dB | −39.7 dB |
| clone gerado do `.ogg` cru | 3.2 kHz | −22.3 dB | −40.3 dB |

Três conclusões:

1. **Clone abafado vindo de WhatsApp é limite da fonte.** Não há brilho acima de
   ~5 kHz para clonar. Só resolve com fonte melhor (Craig) ou enhancer
   generativo.
2. **O `preparation.py` ajuda.** O clone do áudio cru é pior que o do
   processado — mais estreito e com menos energia em 4–8k.
3. **Sample rate não é a causa.** O Pocket TTS é 24 kHz nativo e o
   `get_state_for_audio_prompt` reamostra sozinho. `--sr 24000` já está certo, e
   subir não ganha nada porque a fonte não tem conteúdo lá em cima.

Prefira clipes com pico entre **−6 e −1 dBFS**. Pico em 0.0 dBFS quer dizer que o
AGC do WhatsApp saturou.

## Comandos

| Comando   | O que faz                                                   |
|-----------|-------------------------------------------------------------|
| `/say`    | Fala um texto com a voz escolhida (com autocomplete)         |
| `/voices` | Lista as vozes carregadas                                    |
| `/status` | Ranking de quem mais usa e das vozes preferidas              |
| `/skip`   | Pula só a fala atual                                         |
| `/stop`   | Interrompe e limpa a fila do servidor                        |
| `/leave`  | Desconecta do canal                                          |

O bot entra sozinho no canal de quem chamou `/say`, e sai sozinho quando o canal
esvazia ou após `IDLE_TIMEOUT` segundos sem nada na fila.

## Histórico de uso

Todo comando vira uma linha JSON em `logs/history.jsonl`: quando, quem pediu
(nome, id e apelido), o texto, a voz, o servidor e o canal de voz. É
append-only, então dá para consultar com o bot rodando. O caminho sai de
`HISTORY_FILE`.

```
python history.py summary                # totais por pessoa, voz e dia
python history.py show -n 20             # as últimas 20 falas
python history.py csv -o history.csv     # abre no Excel com acento certo
```

Os três aceitam `--since 2026-08-01 --until 2026-08-31` para recortar o período.

Dentro do Discord, `/status` mostra o mesmo levantamento num embed: pódio de
quem mais usa, vozes preferidas, total de caracteres e o dia mais movimentado.
Aceita `Today`, `Last 7 days`, `Last 30 days` ou `All time` (padrão), e conta
**só o servidor onde foi chamado** — nada vaza de um Discord para outro.

O `logs/` está no `.gitignore` — é conversa das pessoas do servidor e não deve
ser versionado. Avise quem usa o bot que as falas ficam registradas.

## Arquitetura

```
tts_engine.py       modelo + reamostragem 24kHz mono -> 48kHz estéreo
audio_source.py     buffer que alimenta o player do discord.py em streaming
bot.py              slash commands, fila por servidor, fábrica de clientes
voice.py            CLI: clone / say / list
history.py          registro de uso (JSONL) + CLI de relatórios

Panel.bat           atalho: abre o painel sem janela de terminal
Panel.pyw           lançador (mora na raiz para o `import panel` funcionar)
engine.py           processo pesado: carrega o modelo, serve a API na :8081
engine_api.py       rotas do motor: /status, /discord, /say, /import

panel/
  supervisor.py       entrada do painel: bandeja, instância única, serve a :8080
  server.py           rotas do painel, SSE, checagem de origem nas escritas
  process.py          ciclo de vida do motor como subprocesso + log ao vivo
  engine_client.py    conversa HTTP com o motor
  state.py            vozes ocultas e edição do .env por linha
  environment.py      checa o ffmpeg e o python do .venv, e avisa na tela
  static/             index.html, style.css, app.js (sem framework, sem build)

work/pending/       pasta de espera da clonagem (não versionada): o áudio
                    enviado, o _ref.wav e o .safetensors ainda não aprovado
```

O `.safetensors` é a voz (dezenas de MB); o modelo em si são 670 MB em
`~/.cache/huggingface` e serve para todas. Depois do primeiro download, funciona
offline.

**Dois processos, de propósito.** O supervisor é leve — não importa `torch`,
`pocket_tts` nem `discord` — e por isso pode ficar sempre ligado. O motor é o
pesado, e sobe só quando você manda. O motor é dono único do modelo: o lock do
`TTSEngine` só protege dentro de um processo, então uma segunda cópia
corromperia o áudio de duas gerações simultâneas.

### Decisões que valem entender

**Fila por servidor, uma fala por vez.** O modelo é *batch size 1*. Duas
gerações simultâneas corrompem as duas. Um `ThreadPoolExecutor(max_workers=1)`
mais um lock no engine garantem serialização; a fila `asyncio` faz os pedidos
esperarem em vez de falharem.

**A inferência nunca roda no event loop.** PyTorch é síncrono. Chamar direto no
handler congelaria o bot e derrubaria a conexão de voz. Toda geração vai para o
executor via `run_in_executor`.

**Sem FFmpeg.** O Discord quer PCM 48 kHz estéreo 16-bit; o modelo entrega mono
24 kHz float. A conversão é feita com `scipy.signal.resample_poly`, o que
elimina o subprocesso e dá controle exato sobre o buffer.

**Reamostragem com lookahead.** O filtro polifásico precisa de amostras
vizinhas dos dois lados. Reamostrar cada chunk isoladamente produz um clique
audível em cada junção — com uma senoide de 440 Hz, o degrau entre amostras
salta de 0.034 para 0.246 exatamente nas bordas. Por isso o `_StreamResampler`
segura as últimas amostras de cada chunk até a chegada do próximo. Custo:
microssegundos de latência a mais.

**Underrun encerra a reprodução.** Se o gerador travar, `read()` para de esperar
depois de 10 s e devolve `b""`. Sem esse comportamento pegajoso, o bot ficaria
emitindo silêncio no canal indefinidamente.

**O estado da voz é relido do disco a cada fala.** Ler o KV cache do
safetensors é barato e elimina qualquer risco de o estado em memória ser mutado
pela geração anterior. Se você medir isso como gargalo, dá para cachear — mas
confirme antes que `generate_audio_stream` não consome o estado.

## Ajustes

| Sintoma                              | O que mexer                                     |
|--------------------------------------|-------------------------------------------------|
| Comando novo não aparece no Discord  | Preencha `GUILD_ID` no `.env` e reinicie o motor |
| Canal de texto enchendo de anúncio   | `AUTO_DELETE_SECONDS=60` no `.env`              |
| Cortes no meio da fala               | Suba `PREBUFFER_SECONDS` para 1.5–2.0           |
| Demora demais para começar           | Baixe `PREBUFFER_SECONDS`; teste um modelo sem `_24l` |
| Voz robótica ou instável             | Veja `--lsd-decode-steps` e `--temperature` na CLI do Pocket TTS |
| CPU saturada                         | Modelo sem `_24l`; avalie quantização int8      |

### Idiomas

Valores válidos para `TTS_LANGUAGE`. A lista verdadeira é o conteúdo de
`pocket_tts/config/`: o `load_model()` monta o caminho direto do nome.

| Idioma    | Opções                                            |
|-----------|---------------------------------------------------|
| Inglês    | `english`, `english_2026-01`, `english_2026-04`    |
| Português | `portuguese`, `portuguese_24l`                     |
| Espanhol  | `spanish`, `spanish_24l`                           |
| Italiano  | `italian`, `italian_24l`                           |
| Alemão    | `german`, `german_24l`                             |
| Francês   | `french_24l`                                       |

O sufixo `_24l` são 24 camadas no transformer do `flow_lm` em vez de 6 — é a
**única** diferença entre `portuguese.yaml` e `portuguese_24l.yaml`; o resto
(codec Mimi, profundidade do flow, tokenizer) é idêntico. Mais qualidade, mais
CPU por fala.

As vozes ficam presas ao modelo com que foram clonadas: depois de trocar o
`TTS_LANGUAGE`, clone de novo.

## Limitações conhecidas

- Uma geração por vez em todo o processo, não por servidor. Com vários
  servidores ativos, as filas competem. Escalar significa múltiplos processos.
- Sem persistência: a fila vive em memória e some no restart.
- Sem controle de permissão. Qualquer um no servidor pode usar qualquer voz. Se
  isso importar, filtre por cargo em `say()`.
- **Sem controle de emoção ou velocidade.** Ver abaixo.

### Emoção e velocidade: o que o modelo permite

Verificado nas fontes do `pocket_tts` instalado:

- Não existe parâmetro de emoção, velocidade, pitch ou prosódia. Uma busca por
  `emotion|speed|prosod|pitch` no pacote inteiro não retorna nada.
- Os únicos condicionadores são o texto (`conditioners/text.py`) e o prompt de
  áudio. `generate_audio_stream()` aceita só `max_tokens`, `frames_after_eos` e
  `copy_state`.
- Os parâmetros de geração ficam em `load_model()`: `temp` (0.7),
  `lsd_decode_steps`, `noise_clamp`, `eos_threshold`. Viram atributos de
  instância e são lidos a cada geração, então dá para mudar entre falas. Mas
  `temp` controla **variabilidade**, não direção emocional: subir deixa mais
  expressivo e mais instável, não deixa feliz.

**A emoção vem da amostra de referência.** O `.safetensors` carrega a prosódia
de quem falou no WAV. Para ter emoções, clone a mesma pessoa uma vez por emoção
(`alice_feliz`, `alice_triste`) a partir de amostras em que ela realmente fala
daquele jeito.

Velocidade só com pós-processamento: mudar a razão do reamostrador altera o
pitch junto (efeito esquilo); preservar o pitch exige `ffmpeg atempo` no meio do
streaming, o que contraria a decisão de "sem FFmpeg" e adiciona latência.

## Rodando os testes

```bash
pip install -r requirements-dev.txt
python -m pytest
```

A suíte não carrega o modelo e não conecta ao Discord: tudo o que é externo é
substituído por dublês.

## Veja também

[alkmei/discord-tts](https://github.com/alkmei/discord-tts) é um bot mais
completo (Django, Redis, Docker, painel admin para gerenciar vozes), licenciado
sob **AGPL-3.0**.

## Licença

[MIT](LICENSE). O Pocket TTS e os pesos dele têm termos próprios — leia em
https://huggingface.co/kyutai/pocket-tts antes de usar uma voz clonada.
