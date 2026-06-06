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

            if (code === DisconnectReason.loggedOut) {
                console.log("Logged out - restart manually");
                return;
            }

            setTimeout(() => {
                console.log("Reconnecting...");
                start();
            }, 10000);
        }
    });
}

// ================= SEND =================
app.post("/send", async (req, res) => {
    
});


// ================= STATUS =================
app.get("/status", (req, res) => {
    res.json({ connected: isConnected });
});


// ================= QR PAGE =================

// 👇 هذا الأول (صفحة HTML)
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
    <img id="qr" src="/qr-image" />
</div>

<script>
setInterval(() => {
    document.getElementById("qr").src = "/qr-image?time=" + Date.now();
}, 3000);
</script>

</body>
</html>
    `);
});


// 👇 هذا الثاني (الصورة نفسها)
app.get("/qr-image", (req, res) => {
    if (!qrCodeImage) return res.send("QR not ready");
    res.send(`<img src="${qrCodeImage}" style="width:300px"/>`);
});


// ================= START SERVER =================
app.listen(PORT, () => {
    console.log("WhatsApp service running on", PORT);
    start();
});
