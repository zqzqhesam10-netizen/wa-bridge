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

/* ================= START WHATSAPP ================= */
async function start() {
    const { state, saveCreds } = await useMultiFileAuthState("auth");

    sock = makeWASocket({
        auth: state,
        printQRInTerminal: false,
        browser: ["Ubuntu", "Chrome", "120.0.0"]
    });

    sock.ev.on("creds.update", saveCreds);

    sock.ev.on("connection.update", async (update) => {
        const { connection, qr, lastDisconnect } = update;

        if (qr) {
            qrCodeImage = await qrcode.toDataURL(qr);
        }

        if (connection === "open") {
            isConnected = true;
            qrCodeImage = null;

            console.log("✅ WhatsApp Connected");

            // 🔥 جلب الجروبات تلقائيًا عند الاتصال
            const groups = await sock.groupFetchAllParticipating();

            console.log("📌 GROUPS LIST:");
            for (let id in groups) {
                console.log(groups[id].subject + " => " + id);
            }
        }

        if (connection === "close") {
            isConnected = false;

            const code = lastDisconnect?.error?.output?.statusCode;

            console.log("❌ Closed:", code);

            if (code !== DisconnectReason.loggedOut) {
                setTimeout(start, 5000);
            }
        }
    });
}

/* ================= SEND MESSAGE ================= */
app.post("/send", async (req, res) => {
    try {
        const { groupId, text } = req.body;

        if (!sock || !isConnected) {
            return res.status(500).json({ error: "not connected" });
        }

        if (!groupId || !text) {
            return res.status(400).json({ error: "missing data" });
        }

        await sock.sendMessage(groupId, { text });

        res.json({ ok: true });

    } catch (e) {
        console.log(e);
        res.status(500).json({ error: e.message });
    }
});

/* ================= STATUS ================= */
app.get("/status", (req, res) => {
    res.json({
        connected: isConnected,
        hasQR: !!qrCodeImage
    });
});

/* ================= QR PAGE ================= */
app.get("/qr", async (req, res) => {
    if (isConnected) return res.send("<h1>✅ Connected</h1>");
    if (!qrCodeImage) return res.send("<h1>⏳ Loading QR...</h1>");

    res.send(`
        <div style="text-align:center;margin-top:50px">
            <h2>Scan QR Code</h2>
            <img src="${qrCodeImage}" style="width:300px"/>
        </div>
    `);
});

/* ================= START SERVER ================= */
const PORT = process.env.PORT || 10000;

app.listen(PORT, () => {
    console.log("🚀 Server running on", PORT);
    start();
});
