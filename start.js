const { spawn } = require("node:child_process");
const path = require("node:path");

const appPort = String(process.env.PORT || "10000");
const flaskPort = String(process.env.FLASK_INTERNAL_PORT || "5001");
const python = process.env.PYTHON_BIN || "/opt/venv/bin/python";

const children = [];
let stopping = false;

function stopAll(signal, exitCode) {
    if (stopping) return;
    stopping = true;
    process.exitCode = exitCode;
    for (const child of children) {
        if (child && child.exitCode === null && !child.killed) {
            child.kill(signal);
        }
    }
    const forceExit = setTimeout(() => process.exit(exitCode), 5000);
    forceExit.unref();
}

const flask = spawn(
    python,
    [path.join(__dirname, "flask-app", "app.py")],
    {
        env: {
            ...process.env,
            PORT: flaskPort,
            PYTHONUNBUFFERED: "1",
            NODE_URL: `http://127.0.0.1:${appPort}/send`
        },
        stdio: "inherit"
    }
);
children.push(flask);

const bridge = spawn(
    process.execPath,
    [path.join(__dirname, "index.js")],
    {
        env: { ...process.env, PORT: appPort },
        stdio: "inherit"
    }
);
children.push(bridge);

for (const [name, child] of [["Flask helper", flask], ["Node.js bridge", bridge]]) {
    child.on("error", (error) => {
        console.error(`${name} failed to start:`, error);
        stopAll("SIGTERM", 1);
    });
    child.on("exit", (code, signal) => {
        if (!stopping) {
            console.error(`${name} exited (code=${code}, signal=${signal}); stopping both processes.`);
            stopAll("SIGTERM", 1);
        }
    });
}

process.on("SIGTERM", () => stopAll("SIGTERM", 0));
process.on("SIGINT", () => stopAll("SIGINT", 0));
