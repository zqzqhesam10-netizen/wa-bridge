const express = require("express");
const { default: makeWASocket, useMultiFileAuthState } = require("@whiskeysockets/baileys");

const app = express();
app.use(express.json());

let sock;

async function start() {
    const { state, saveCreds } = await useMultiFileAuthState("auth");

    sock = makeWASocket({
        auth: state,
        printQRInTerminal: true
    });

    sock.ev.on("creds.update", saveCreds);

    console.log("WhatsApp Bridge Ready");
}

start();

app.post("/send", async (req, res) => {
    try {
        const { image, caption } = req.body;

        await sock.sendMessage(process.env.GROUP_ID, {
            image: { url: image },
            caption: caption
        });

        res.json({ status: "sent" });

    } catch (e) {
        console.log(e);
        res.status(500).json({ error: "failed" });
    }
});

app.get("/ping", (req, res) => {
    res.send("OK");
});

const PORT = process.env.PORT || 3000;
app.listen(PORT, () => {
    console.log("Running on port", PORT);
});
