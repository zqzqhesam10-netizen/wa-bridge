const express = require("express");
const fs = require("fs");
const qrcode = require("qrcode");
const {
    default: makeWASocket,
    useMultiFileAuthState,
    DisconnectReason
} = require("@whiskeysockets/baileys");

const app = express();
app.use(express.json());

let sock;
let qrCodeImage = null;
let isConnected = false;

// ================= CLEAN OLD SESSION =================
try {
    fs.rmSync("auth", { recursive: true, force: true });
    console.log("Old auth cleared");
} catch (e) {}

// ================= START WHATSAPP =================
async function start() {
    const { state, saveCreds } = await useMultiFileAuthState("auth");

    sock = makeWASocket({
        auth: state,
        printQRInTerminal: false,
        browser: ["Ubuntu", "Chrome", "120.0.0"],
        markOnlineOnConnect: false,
        keepAliveIntervalMs: 25000
    });

    sock.ev.on("creds.update", saveCreds);

    sock.ev.on("connection.update", async (update) => {
        const { connection, qr, lastDisconnect } = update;

        if (qr) {
            console.log("QR GENERATED");
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

            if (code === DisconnectReason.loggedOut) {
                console.log("Logged out - تحتاج إعادة مسح QR");
                return;
            }

            setTimeout(start, 5000);
        }
    });
}

// ================= SEND MESSAGE =================
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
        console.log(e);
        res.status(500).json({ error: e.message });
    }
});

// ================= STATUS =================
app.get("/status", (req, res) => {
    res.json({ connected: isConnected });
});

// ================= QR IMAGE =================
app.get("/qr-image", (req, res) => {
    if (!qrCodeImage) {
        return res.status(404).send("no-qr");
    }

    const base64 = qrCodeImage.replace("data:image/png;base64,", "");
    const img = Buffer.from(base64, "base64");

    res.writeHead(200, {
        "Content-Type": "image/png",
        "Cache-Control": "no-store"
    });

    res.end(img);
});

// ================= QR PAGE =================
app.get("/qr", (req, res) => {
    res.send(`
<!DOCTYPE html>
<html>
<head>
    <title>WhatsApp QR</title>
    <meta charset="UTF-8">
    <style>
        body {
            background: #111;
            color: white;
            text-align: center;
            font-family: Arial;
            padding-top: 50px;
        }
        img {
            margin-top: 20px;
            border: 5px solid #25D366;
            border-radius: 12px;
        }
        .box {
            background: #222;
            padding: 20px;
            display: inline-block;
            border-radius: 12px;
        }
    </style>
</head>
<body>

<div class="box">
    <h2>📱 WhatsApp QR Login</h2>
    <p>امسح الكود لتسجيل الدخول</p>
    <img id="qr" src="/qr-image?t=1" />
</div>

<script>
setInterval(() => {
    document.getElementById("qr").src = "/qr-image?t=" + Date.now();
}, 3000);
</script>

</body>
</html>
    `);
});

// ================= START SERVER =================
const PORT = process.env.PORT || 3001;

app.listen(PORT, () => {
    console.log("Server running on", PORT);
    start();
});
