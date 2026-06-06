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

// ================= دالة بدء واتساب =================
async function start() {
    const { state, saveCreds } = await useMultiFileAuthState("auth");

    sock = makeWASocket({
        auth: state,
        printQRInTerminal: false,
        browser: ["Chrome (Linux)", "Chrome", "120.0.0"],
        keepAliveIntervalMs: 25000
    });

    sock.ev.on("creds.update", saveCreds);

    sock.ev.on("connection.update", async (update) => {
        const { connection, qr, lastDisconnect } = update;

        // تحديث صورة الكود
        if (qr) {
            qrCodeImage = await qrcode.toDataURL(qr);
        }

        if (connection === "open") {
            console.log("✅ متصل");
            qrCodeImage = null; 
            isConnected = true;
        }

        if (connection === "close") {
            isConnected = false;
            qrCodeImage = null; // مسح الكود عند الانقطاع

            const shouldReconnect = lastDisconnect?.error?.output?.statusCode !== DisconnectReason.loggedOut;
            console.log("❌ تم الإغلاق. هل يجب إعادة الاتصال؟", shouldReconnect);

            if (shouldReconnect) {
                setTimeout(() => start(), 5000);
            }
        }
    });
}

// ================= المسارات (Routes) =================
app.get("/status", (req, res) => {
    res.json({ connected: isConnected });
});

app.get("/qr-image", (req, res) => {
    res.setHeader("Cache-Control", "no-cache, no-store, must-revalidate");

    if (!qrCodeImage) {
        const empty = Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO7W2h0AAAAASUVORK5CYII=", "base64");
        res.writeHead(200, { "Content-Type": "image/png" });
        return res.end(empty);
    }

    const img = qrCodeImage.replace("data:image/png;base64,", "");
    res.writeHead(200, { "Content-Type": "image/png" });
    res.end(Buffer.from(img, "base64"));
});

app.get("/qr", (req, res) => {
    res.send(`
    <html>
    <body style="background:#0f0f0f; color:white; text-align:center; font-family:Arial; padding-top:50px;">
        <div style="background:#1e1e1e; padding:20px; display:inline-block; border-radius:15px;">
            <h2>WhatsApp QR Login</h2>
            <img id="qr" src="/qr-image?t=${Date.now()}" style="width:250px; background:white; padding:5px; border-radius:10px;" />
            <p id="status">جاري التحقق...</p>
        </div>
        <script>
            setInterval(() => document.getElementById("qr").src = "/qr-image?t=" + Date.now(), 2000);
            setInterval(async () => {
                const res = await fetch("/status");
                const data = await res.json();
                document.getElementById("status").innerText = data.connected ? "🟢 متصل" : "🟡 بانتظار الكود";
            }, 2000);
        </script>
    </body>
    </html>`);
});

const PORT = process.env.PORT || 10000;
app.listen(PORT, () => {
    console.log("🚀 الخادم يعمل على المنفذ", PORT);
    start();
});
