const { Client, MessageMedia } = require("./index");
const qrcode = require("qrcode-terminal");
const express = require("express");
const fs = require("fs");
const path = require("path");

const PORT = 3737;
const app = express();
app.use(express.json({ limit: "200mb" }));

// ── encontra o Chrome/Edge instalado no sistema ───────────────────────────────
const possivelChrome = [
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
  "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
  (process.env.LOCALAPPDATA || "") + "\\Google\\Chrome\\Application\\chrome.exe",
  "C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe",
  "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
];
const chromePath = possivelChrome.find((p) => fs.existsSync(p));
if (!chromePath) {
  console.error("\n[ERRO] Chrome ou Edge nao encontrado!\nInstale o Google Chrome: https://www.google.com/chrome/\n");
  process.exit(1);
}
console.log(`[puppeteer] usando: ${chromePath}`);

// ── cliente WhatsApp (sem persistencia - exige QR code a cada inicio) ────────
const client = new Client({
  // Sem authStrategy = sem salvar sessao. QR code sera exigido toda vez.
  puppeteer: {
    executablePath: chromePath,
    args: ["--no-sandbox", "--disable-setuid-sandbox"],
  },
});

let pronto = false;

client.on("qr", (qr) => {
  console.log("\nEscaneie o QR code abaixo com seu WhatsApp:\n");
  qrcode.generate(qr, { small: true });
});

client.on("ready", () => {
  pronto = true;
  console.log("\n[whatsapp] conectado e pronto!\n");
});

client.on("disconnected", (reason) => {
  pronto = false;
  console.log(`[whatsapp] desconectado: ${reason}`);
});

client.initialize();

// ── helper: resolve numero com fix de LID ────────────────────────────────────
async function resolverNumero(numero) {
  // grupos: retorna direto
  if (numero.includes("@g.us")) return numero;

  const limpo = numero.replace(/\D/g, "");
  const chatId = `${limpo}@c.us`;

  // tenta verificar se o numero existe e pegar o LID correto
  try {
    const contato = await client.getContactById(chatId);
    // se tiver LID disponivel, usa ele
    if (contato && contato.id && contato.id._serialized) {
      return contato.id._serialized;
    }
  } catch (e) {
    // ignora e tenta direto
  }

  return chatId;
}

// ── helper: envia com fallback de LID ────────────────────────────────────────
async function enviarMensagem(numero, conteudo, opcoes = {}) {
  const chatId = await resolverNumero(numero);
  try {
    return await client.sendMessage(chatId, conteudo, opcoes);
  } catch (e) {
    if (e.message && e.message.includes("No LID")) {
      // tenta com numero sem o 9 (alguns numeros antigos nao tem o 9)
      const limpo = numero.replace(/\D/g, "");
      if (limpo.length === 13 && limpo[4] === "9") {
        const semNove = limpo.slice(0, 4) + limpo.slice(5);
        console.log(`[whatsapp] tentando sem o 9: ${semNove}`);
        return await client.sendMessage(`${semNove}@c.us`, conteudo, opcoes);
      }
      // tenta com o 9 se nao tinha
      if (limpo.length === 12) {
        const comNove = limpo.slice(0, 4) + "9" + limpo.slice(4);
        console.log(`[whatsapp] tentando com o 9: ${comNove}`);
        return await client.sendMessage(`${comNove}@c.us`, conteudo, opcoes);
      }
    }
    throw e;
  }
}

// ── rotas ─────────────────────────────────────────────────────────────────────

app.get("/status", (req, res) => {
  res.json({ pronto });
});

app.post("/texto", async (req, res) => {
  if (!pronto) return res.status(503).json({ erro: "WhatsApp nao conectado" });
  const { numero, texto } = req.body;
  if (!numero || !texto)
    return res.status(400).json({ erro: "numero e texto sao obrigatorios" });
  try {
    await enviarMensagem(numero, texto);
    res.json({ ok: true });
  } catch (e) {
    console.error("[whatsapp] erro ao enviar texto:", e.message);
    res.status(500).json({ erro: e.message });
  }
});

app.post("/arquivo", async (req, res) => {
  if (!pronto) return res.status(503).json({ erro: "WhatsApp nao conectado" });
  const { numero, caminho, legenda } = req.body;
  if (!numero || !caminho)
    return res.status(400).json({ erro: "numero e caminho sao obrigatorios" });
  if (!fs.existsSync(caminho))
    return res.status(404).json({ erro: `arquivo nao encontrado: ${caminho}` });
  try {
    const media = MessageMedia.fromFilePath(caminho);
    await enviarMensagem(numero, media, { caption: legenda || "", sendMediaAsDocument: true });
    console.log(`[whatsapp] enviado: ${path.basename(caminho)}`);
    res.json({ ok: true });
  } catch (e) {
    console.error("[whatsapp] erro ao enviar arquivo:", e.message);
    res.status(500).json({ erro: e.message });
  }
});

app.get("/grupos", async (req, res) => {
  if (!pronto) return res.status(503).json({ erro: "WhatsApp nao conectado" });
  try {
    const chats = await client.getChats();
    const grupos = chats
      .filter((c) => c.isGroup)
      .map((c) => ({ nome: c.name, id: c.id._serialized }));
    res.json(grupos);
  } catch (e) {
    res.status(500).json({ erro: e.message });
  }
});

app.listen(PORT, () => {
  console.log(`[servidor] rodando em http://localhost:${PORT}`);
  console.log("[servidor] aguardando QR code do WhatsApp...\n");
});
