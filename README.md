# 🤖 Jarvis — assistente de voz para o Discord

Um bot que fica na call com você e responde **falando** quando você chama ele
pelo nome:

> **"Jarvis, qual a capital da Austrália?"** → *"A capital da Austrália é Camberra."*

A pergunta vai como áudio para o **Gemini Live** (Google), que responde em voz.
A resposta começa a tocar em ~2–3 segundos, enquanto ainda está chegando, e
também aparece escrita no chat.

- [Comandos](#comandos)
- [Como usar na call](#como-usar-na-call)
- [Como funciona](#como-funciona)
- [Instalação](#instalação)
- [Rodando numa VM (24h no ar)](#rodando-numa-vm-24h-no-ar)
- [Configuração](#configuração)
- [Testes](#testes)
- [Estrutura](#estrutura)

## Comandos

| Comando | O que faz |
| --- | --- |
| `/entrar` | Entra no canal de voz em que você está e fica ouvindo |
| `/sair` | Sai da call (ou peça por voz: *"Jarvis, sai da call"*) |
| `/notas` | Mostra as suas anotações feitas por voz (com `apagar`, apaga todas) |
| `/esquecer` | Apaga a memória da conversa neste servidor, na hora |
| `/lembretes` | Mostra os seus lembretes pendentes (com `cancelar`, apaga todos) |
| `/voz` | Troca a voz do bot neste servidor, na hora (fica salva em `data/vozes.json`) |
| `/status` | Como o bot está neste servidor: call, voz, memória, lembretes, ping |
| `/ajuda` | O guia, dentro do Discord |

## Como usar na call

- **Perguntar:** *"Jarvis, …"* e a pergunta. Pode pausar no meio para pensar:
  se você voltar a falar logo depois, o bot para, espera você terminar e
  responde a pergunta inteira.
- **Chamar e esperar:** *"Jarvis…"* (toca um estalo de "estou ouvindo") e aí
  a pergunta, em até 6 segundos.
- **Modo conversa:** depois da resposta, você tem 8 segundos para continuar
  *sem* repetir o nome (*"e a de Portugal?"*).
- **Interromper:** *"Jarvis, para"* (ou *"esquece"*, *"cancela"*, *"tá bom"*,
  *"deixa pra lá"*), ou só *"Jarvis"*, corta a resposta. Uma pergunta nova também.
- **Mandar embora:** do jeito que você falaria com uma pessoa. As formas curtas
  e comuns saem **na hora**: *"sai da call"*, *"tchau"*, *"cai fora"*, *"vaza
  daqui"*, *"se manda"*, *"tá dispensado"*, *"ninguém te chamou"*… O resto a IA
  entende: *"a gente quer conversar a sós, dá licença"* ou até *"quando eu falar
  Lula, você sai"* — o bot se despede e sai. Perguntas que só *parecem* pedido
  (*"sai mais barato ir de ônibus?"*, *"como se diz tchau em inglês?"*)
  continuam sendo perguntas.
- **Memória:** o bot lembra das últimas 5 trocas por 10 minutos (*"e a de
  Portugal?"* depois da capital da França). Sair da call ou o `/esquecer` zera.
- O bot sabe **que dia e que horas são** e **com quem está falando** (o apelido
  no servidor).
- **Internet:** para o que muda com o tempo (notícias, placares, cotações,
  clima), ele pesquisa no Google antes de responder.
- **Sorteios de verdade:** *"Jarvis, joga um dado"*, *"sorteia três números de
  1 a 60"*, *"cara ou coroa?"*, *"quem começa, eu ou o Pedro?"*. O resultado
  sai de um sorteio no computador, não da cabeça da IA.
- **Hora pelo mundo:** *"Jarvis, que horas são em Tóquio?"*, com o horário de
  verão de cada lugar.
- **Datas:** *"Jarvis, quantos dias faltam pro Natal?"*, *"que dia da semana
  cai 15 de novembro?"*.
- **Contas certas:** *"Jarvis, quanto é 15% de 1.250?"*, *"raiz de 2 vezes 7"*.
  A conta sai de uma calculadora, não da cabeça da IA (que erra).
- **Enquetes:** *"Jarvis, faz uma enquete: pizza, hambúrguer ou japonês?"*. Vai
  para o chat com uma reação numerada por opção (o bot precisa poder reagir).
- **Anotações:** *"Jarvis, anota: comprar pão"*, *"o que eu anotei?"*,
  *"apaga minhas notas"*. Ficam salvas em `data/notas.json`; o `/notas` mostra.
- **Quem está na call:** *"Jarvis, quem tá aqui?"*, *"sorteia alguém da call
  pra começar"*.
- **Lembretes:** *"Jarvis, me avisa em 10 minutos pra tirar a pizza"*. Na hora,
  o bot marca você no chat (até 24 horas). Os pendentes ficam salvos em
  `data/lembretes.json` e voltam se o bot reiniciar. Por voz também dá para
  perguntar *"quais são meus lembretes?"* e pedir *"cancela meus lembretes"*.
- Se todo mundo sair da call, ele sai sozinho depois de 2 minutos.

O bot só conversa: não toca música, não manda recado para os outros e não mexe no Discord —
se pedirem, ele diz que não consegue.

## Como funciona

```
fala na call ──► o Whisper transcreve e procura o nome no começo da frase
                 (pelo SOM: "Jávez", "Jardes", "D'Arvis" também valem)
             ──► ouviu o nome? estalo de "estou ouvindo"
             ──► comando curto ("para", "sai da call")? resolve na hora, sem IA
             ──► o resto vai como ÁUDIO ao Gemini Live, que entende a fala
                 melhor que qualquer transcrição
             ──► a resposta em voz toca na call enquanto ainda está chegando
```

- **O nome pelo som:** o Whisper quase nunca escreve "Jarvis" — escreve
  "Jávez", "Jardes", "Já arbise"… O nome é comparado pelo som (b/v, z/s, e/i no
  fim…), não pela grafia. Com 4 vozes e o áudio comprimido como no Discord: 23
  de 24 frases reconhecidas e nenhum alarme falso em 15 frases sem o nome ("Já
  vi esse filme" parecia "Jarvis"). Limitação conhecida: nomes que soam igual
  ("Jarbas", "Juarez") também acordam o bot.
- **Nome no meio da frase não conta:** *"não, do jarvis lá"* é conversa entre
  as pessoas, e o bot não se mete.
- **Pausa no meio da pergunta:** a frase fecha depois de 0,8s de silêncio. Se
  quem perguntou voltar a falar logo, o pedido é refeito com a pergunta inteira.
- **Idioma fixo:** a transcrição vai com o idioma da call (`VOICE_IDIOMA=pt`).
  Adivinhando sozinha, a Groq escreveu 1 em cada 4 falas de uma call brasileira
  em outro idioma. As frases que o Whisper inventa sobre ruído ("Thank you.",
  "Obrigado por assistir") não viram pergunta, e outros bots da call não são
  escutados.
- **Sair pela IA:** o Gemini tem uma única ferramenta, `sair_da_call`. Quando
  entende que mandaram o bot embora, ele a usa, fala a despedida e o bot sai.
- **Privacidade:** o bot escuta a call inteira para ouvir o nome, mas só a fala
  de quem chamou vai ao Google. O resto fica na máquina (Whisper local) ou vai
  só para a transcrição da Groq. No tier grátis, o Google pode usar o que
  recebe para melhorar os modelos.

## Instalação

### 1. O que você precisa

- **Python 3.11+**
- **Um bot no [Discord Developer Portal](https://discord.com/developers/applications):**
  - **Bot → Reset Token** (guarde o token)
  - **OAuth2 → URL Generator:** scopes `bot` + `applications.commands`;
    permissões `View Channels`, `Send Messages`, `Embed Links`,
    `Read Message History`, `Connect`, `Speak` e `Use Voice Activity`. Abra a
    URL gerada e convide o bot para o seu servidor.
- **Uma chave grátis do Gemini:** [aistudio.google.com/apikey](https://aistudio.google.com/apikey)
- **(Opcional, recomendado) uma chave grátis da Groq:**
  [console.groq.com](https://console.groq.com). A transcrição na nuvem
  reconhece o nome bem melhor, e é o que deixa o bot leve numa máquina fraca.

### 2. Instalar

Windows (PowerShell):

```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy .env.example .env
```

Linux:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
```

Depois, edite o `.env`: `DISCORD_TOKEN` e `GEMINI_API_KEY` são obrigatórios;
`GROQ_API_KEY`, `BOT_NAME` e `GUILD_ID` são opcionais (ver
[Configuração](#configuração)).

### 3. Rodar

```powershell
.venv\Scripts\python src\main.py      # Windows
```

```bash
.venv/bin/python src/main.py          # Linux
```

Quando aparecer `✅ Online como …`, entre num canal de voz, mande `/entrar` e
fale *"Jarvis, …"*.

## Rodando numa VM (24h no ar)

Para o bot ficar online sem o seu computador ligado, rode numa VM Linux (por
exemplo, uma e2-micro do Google Cloud) como um serviço do `systemd`: ele sobe
sozinho quando a VM liga e volta se cair.

**1. Instale na VM** seguindo o passo [Instalar](#2-instalar) do Linux, numa
pasta como `~/bot`. Se a VM ainda não tiver, instale antes o `venv` e a
biblioteca de áudio do Discord:

```bash
sudo apt update && sudo apt install -y python3-venv libopus0
```

**2. Crie o serviço** em `/etc/systemd/system/bot.service` (troque `SEU_USUARIO`
pelo resultado de `whoami`):

```ini
[Unit]
Description=Discord voice bot
After=network-online.target
Wants=network-online.target

[Service]
User=SEU_USUARIO
WorkingDirectory=/home/SEU_USUARIO/bot
ExecStart=/home/SEU_USUARIO/bot/.venv/bin/python src/main.py
Environment=PYTHONUNBUFFERED=1
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now bot
```

**3. Comandos do dia a dia:**

| Para… | Comando |
| --- | --- |
| Ver se está rodando | `systemctl status bot` |
| Acompanhar o log ao vivo | `journalctl -u bot -f` (Ctrl+C para sair) |
| Reiniciar | `sudo systemctl restart bot` |
| Parar | `sudo systemctl stop bot` |

**4. Atualizar o código.** No seu computador, empacote a pasta `src`:

```bash
tar --exclude=__pycache__ --exclude=.ruff_cache -czf jarvis-src.tar.gz src
```

Envie o arquivo para a VM (no Google Cloud, pelo botão **SSH → UPLOAD FILE**)
e troque o código, guardando uma cópia da versão anterior:

```bash
cd ~/bot
rm -rf src.bak && cp -r src src.bak
tar -xzf ~/jarvis-src.tar.gz
sudo systemctl restart bot
```

Deu errado? Volte para a versão anterior:

```bash
cd ~/bot && rm -rf src && mv src.bak src && sudo systemctl restart bot
```

Se o `requirements.txt` mudar, rode também `.venv/bin/pip install -r
requirements.txt` antes de reiniciar.

> **Máquina fraca:** numa VM pequena, use a Groq (`GROQ_API_KEY`). O detector
> local do nome se mede e, se ficar lento, se desliga sozinho —
> `WHISPER_DETECTOR=off` desliga de cara.

## Configuração

Tudo fica no `.env` (o [`.env.example`](.env.example) explica cada opção).

| Variável | Padrão | O que faz |
| --- | --- | --- |
| `DISCORD_TOKEN` | — | **Obrigatória.** O token do bot |
| `GEMINI_API_KEY` | — | **Obrigatória.** Quem responde as perguntas |
| `BOT_NAME` | `Jarvis` | O nome que chama o bot (uma palavra) |
| `GUILD_ID` | vazio | Comandos aparecem na hora, mas só neste servidor. Vazio: em todos, mas a primeira vez leva até 1h |
| `GROQ_API_KEY` | vazio | Transcrição na nuvem (melhor e mais leve). Sem ela, usa o Whisper local |
| `WHISPER_MODEL` | `base` | Whisper local: `tiny` (rápido), `base`, `small` (preciso) |
| `VOICE_IDIOMA` | `pt` | Idioma da call para a transcrição |
| `GEMINI_VOZ` | padrão | A voz do bot: `Puck`, `Zephyr`, `Kore`, `Charon`… |
| `FUSO` | `America/Sao_Paulo` | Fuso usado para dizer que horas são |
| `VOICE_CONVERSA_S` | `8` | Segundos do modo conversa (`0` desliga) |
| `RESPOSTAS_NO_CHAT` | `1` | `0`: o bot só fala, sem escrever no chat |
| `VOICE_SOM_ESCUTA` | `1` | `0`: sem o estalo de "estou ouvindo" |
| `VOICE_MIN_RMS` | `250` | Volume mínimo para contar como fala. Suba se o bot reage a barulho |
| `VOICE_DEBUG` | `0` | `1`: loga o texto transcrito e grava os trechos (só para diagnóstico) |

Se trocar `GUILD_ID` para vazio depois de usar um servidor fixo,
`src/migrar_para_global.py --apagar` remove os comandos antigos do servidor.

### Limites do Gemini (tier grátis)

O `gemini-3.8-live` no tier grátis tem requisições ilimitadas por minuto e por
dia, e 65 mil tokens por minuto (≈ 30 minutos de áudio por minuto — sobra para
uma call). Os números da sua chave aparecem em
[aistudio.google.com/rate-limit](https://aistudio.google.com/rate-limit).

## Testes

```powershell
.venv\Scripts\pip install -r requirements-dev.txt
.venv\Scripts\python -m pytest
```

Os testes rodam sem rede, sem Discord e sem chaves (o `tests/conftest.py`
ignora o seu `.env`). O `tests/test_static.py` roda o ruff, e o GitHub Actions
(`.github/workflows/tests.yml`) repete tudo a cada push.

## Estrutura

```
src/
├── main.py                 # ponto de entrada
├── config.py               # lê o .env
├── messages.py             # TODOS os textos do bot
├── cogs/                   # /entrar, /sair, /ajuda, ciclo de vida, registro de uso
├── core/
│   ├── assistente.py       # um por servidor: conexão de voz, fala do bot, sair sozinho
│   ├── listen.py           # a escuta: trechos de fala, o nome, a pergunta montada
│   ├── intents.py          # o nome (pelo som) e os comandos instantâneos
│   ├── audio.py            # a fala ao vivo e o estalo de "estou ouvindo"
│   └── pycord_voice_patch.py  # conserta a recepção de voz do py-cord 2.8.1
└── services/
    ├── gemini.py           # Gemini Live: pergunta em áudio → resposta em voz
    ├── speech.py           # Whisper (Groq ou local) para achar o nome
    └── observabilidade.py  # logging com segredos mascarados
```

A recepção de voz do py-cord 2.8.1 está quebrada upstream (issue #3139,
DAVE/E2EE); `src/core/pycord_voice_patch.py` corrige o que falta. Por isso as
versões do `requirements.txt` são exatas: atualizar o py-cord é uma decisão
consciente, testada numa call. Remova o patch quando o py-cord fechar o issue.

O `bot.py` na raiz é uma versão antiga, só de texto (API da NVIDIA), e não é
usado pelo Jarvis.

---

A base (entrar na call, capturar e segmentar o áudio, o patch de voz, o
Whisper) veio do bot de música "Cabrunco", de um amigo — o histórico do git
guarda o bot de música inteiro.
