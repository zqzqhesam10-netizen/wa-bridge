const express = require("express");
const {
    default: makeWASocket,
    useMultiFileAuthState,
    DisconnectReason
} = require("@whiskeysockets/baileys");

const qrcode = require("qrcode");

const app = express();
app.use(express.json());

let sock;
let qrCodeImage = null;
let isConnected = false;

async function start() {
    const { state, saveCreds } = await useMultiFileAuthState("auth");

    sock = makeWASocket({
        auth: state,
        printQRInTerminal: false,
        browser: ["Ubuntu", "Chrome", "120.0.0"],
        keepAliveIntervalMs: 25000
    });

    sock.ev.on("creds.update", saveCreds);

    sock.ev.on("connection.update", async (update) => {
        const { connection, qr, lastDisconnect } = update;

        if (qr) {
            qrCodeImage = await qrcode.toDataURL(qr);
        }

        if (connection === "open") {
            console.log("WhatsApp Connected");
            qrCodeImage = null;
            isConnected = true;
        }

        if (connection === "close") {
            isConnected = false;
            const code = lastDisconnect?.error?.output?.statusCode;

            console.log("Closed:", code);

            setTimeout(start, 10000);
        }
    });
}

start();

// ================= SEND =================
app.post("/send", async (req, res) => {
    try {
        const { groupId, image, caption } = req.body;

        if (!sock || !isConnected) {
            return res.status(500).json({ error: "not connected" });
        }

        await sock.sendMessage(groupId, {
            image: { url: image },
            caption: caption || ""
        });

        res.json({ ok: true });

    } catch (e) {
        res.status(500).json({ error: e.message });
    }
});

// QR
app.get("/qr", (req, res) => {
    if (!qrCodeImage) return res.send("QR not ready");
    res.send(`<img src="${qrCodeImage}"/>`);
});

app.get("/status", (req, res) => {
    res.json({ connected: isConnected });
});

app.listen(3001, () => console.log("WhatsApp service running"));