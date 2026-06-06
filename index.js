const {
    default: makeWASocket,
    useMultiFileAuthState,
    DisconnectReason
} = require("@whiskeysockets/baileys");
const pino = require("pino");

async function start() {
    const { state, saveCreds } = await useMultiFileAuthState("./auth_info");

    const sock = makeWASocket({
        auth: state,
        logger: pino({ level: "silent" }),
        // إعدادات لضمان استقرار الاتصال في البيئات السحابية
        connectTimeoutMs: 60000,
        defaultQueryTimeoutMs: 60000,
        keepAliveIntervalMs: 30000,
        browser: ["WhatsApp Server", "Chrome", "125.0.0"]
    });

    sock.ev.on("creds.update", saveCreds);

    sock.ev.on("connection.update", (update) => {
        const { connection, lastDisconnect, qr } = update;

        if (qr) {
            console.log("📌 QR Code تم توليده، يرجى مسحه من الـ Logs");
            // في حال كنت تستخدم مكتبة qrcode لعرضه كنص
            require('qrcode-terminal').generate(qr, {small: true});
        }

        if (connection === "open") {
            console.log("✅ تم الاتصال بنجاح!");
        }

        if (connection === "close") {
            const shouldReconnect = lastDisconnect?.error?.output?.statusCode !== DisconnectReason.loggedOut;
            console.log("❌ انقطع الاتصال. إعادة محاولة الربط خلال 30 ثانية...");
            if (shouldReconnect) {
                setTimeout(start, 30000); // زيادة وقت الانتظار لتجنب الحظر
            }
        }
    });
}

start();
