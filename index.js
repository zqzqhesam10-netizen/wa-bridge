const {
    default: makeWASocket,
    useMultiFileAuthState,
    DisconnectReason
} = require("@whiskeysockets/baileys");
const pino = require("pino");

async function start() {
    // استخدام مجلد مؤقت للبيانات (مناسب لـ Render)
    const { state, saveCreds } = await useMultiFileAuthState("/tmp/auth");

    const sock = makeWASocket({
        auth: state,
        logger: pino({ level: "silent" }), // إخفاء السجلات غير الضرورية
        printQRInTerminal: true, // طباعة الكود مباشرة في الـ Console
        browser: ["WhatsApp Bot", "Chrome", "1.0.0"]
    });

    sock.ev.on("creds.update", saveCreds);

    sock.ev.on("connection.update", (update) => {
        const { connection, lastDisconnect } = update;

        if (connection === "open") {
            console.log("✅ تم الاتصال بنجاح بخدمة واتساب.");
        }

        if (connection === "close") {
            const shouldReconnect = lastDisconnect?.error?.output?.statusCode !== DisconnectReason.loggedOut;
            console.log("❌ تم إغلاق الاتصال. محاولة إعادة الاتصال...");
            
            if (shouldReconnect) {
                setTimeout(start, 5000);
            }
        }
    });

    sock.ev.on("messages.upsert", async (m) => {
        // هنا يمكنك إضافة منطق التعامل مع الرسائل المستلمة
        console.log("📥 رسالة جديدة مستلمة");
    });
}

start();
