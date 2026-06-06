const express = require("express");
const {
    default: makeWASocket,
    useMultiFileAuthState,
    DisconnectReason
} = require("@whiskeysockets/baileys");

const qrcode = require("qrcode");
const axios = require("axios");
const cheerio = require("cheerio");

const app = express();
app.use(express.json());

// ================= STATE =================
let sock;
let qrCodeImage = null;
let isConnected = false;

// منع التكرار
let sentLinks = new Set();

// ================= GROUP =================
const GROUP_ID = process.env.GROUP_ID || "CxG1mLQR5VtGhiZaaAMqyI@g.us";

// ================= WHATSAPP START =================
async function start() {
    const { state, saveCreds } = await useMultiFileAuthState("auth");

    sock = makeWASocket({
        auth: state,
        printQRInTerminal: false,
        browser: ["Render Bot", "Chrome", "1.0.0"]
    });

    sock.ev.on("creds.update", saveCreds);

    sock.ev.on("connection.update", async (update) => {
        const { connection, qr, lastDisconnect } = update;

        if (qr) {
            qrCodeImage = await qrcode.toDataURL(qr);
            console.log("📌 QR Updated");
        }

        if (connection === "open") {
            console.log("✅ WhatsApp Connected");
            qrCodeImage = null;
            isConnected = true;
        }

        if (connection === "close") {
            isConnected = false;

            const reason = lastDisconnect?.error?.output?.statusCode;
            console.log("❌ Connection closed:", reason);

            if (reason === DisconnectReason.loggedOut) {
                process.exit(1);
            } else {
                setTimeout(start, 3000);
            }
        }
    });

    console.log("🚀 WhatsApp Bridge Running");
}

start();

// ================= SEND =================
async function sendToGroup(image, caption) {
    if (!sock || !isConnected) return;

    try {
        await sock.sendMessage(GROUP_ID, {
            image: { url: image },
            caption: caption || ""
        });

        console.log("📤 Sent");
    } catch (e) {
        console.log("SEND ERROR:", e.message);
    }
}

// ================= SCRAPER =================
async function checkUpdates() {
    try {
        console.log("🔎 Checking...");

        const res = await axios.get("https://tuktukhd.com/recent/");
        const $ = cheerio.load(res.data);

        let count = 0;

        $("a").each(async (i, el) => {

            if (count >= 5) return;

            const img =
                $(el).find("img").attr("src") ||
                $(el).find("img").attr("data-src");

            const title =
                $(el).attr("title") ||
                $(el).find("img").attr("alt") ||
                "جديد";

            const link = $(el).attr("href");

            if (!img || !link) return;

            if (sentLinks.has(link)) return;

            sentLinks.add(link);

            const msg = `📺 ${title}\n🔥 جديد الآن`;

            await sendToGroup(img, msg);

            count++;
        });

    } catch (e) {
        console.log("SCRAPER ERROR:", e.message);
    }
}

// ================= AUTO RUN =================
setInterval(checkUpdates, 60 * 1000);

// ================= ROUTES =================

// الحالة
app.get("/", (req, res) => {
    res.send("WhatsApp Bridge Running ✅");
});

// QR
app.get("/qr", (req, res) => {
    if (!qrCodeImage) {
        return res.send("<h3>Connected or QR not ready</h3>");
    }

    res.send(`
        <html>
        <body style="text-align:center">
            <h2>Scan QR</h2>
            <img src="${qrCodeImage}" />
        </body>
        </html>
    `);
});

// ================= CONTROL =================

// فحص يدوي
app.get("/check-now", async (req, res) => {
    try {
        await checkUpdates();
        res.json({ status: "checked", ok: true });
    } catch (e) {
        res.status(500).json({ error: e.message });
    }
});

// مسح الذاكرة (منع التكرار)
app.get("/reset", (req, res) => {
    sentLinks.clear();
    res.json({ status: "memory cleared", ok: true });
});

// Reset + Check
app.get("/reset-check", async (req, res) => {
    try {
        sentLinks.clear();
        await checkUpdates();
        res.json({ status: "reset + checked", ok: true });
    } catch (e) {
        res.status(500).json({ error: e.message });
    }
});

// ping
app.get("/ping", (req, res) => res.send("OK"));

// ================= START =================
const PORT = process.env.PORT || 3000;
app.listen(PORT, () => console.log("Server running on", PORT));
