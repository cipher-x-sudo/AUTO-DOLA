const { app, BrowserWindow, ipcMain, dialog, shell, Menu } = require('electron');
const path = require('path');
const fs = require('fs');
const { spawn, exec } = require('child_process');
const http = require('http');

// Auto-updater
const { autoUpdater } = require('electron-updater');

// App info
const APP_NAME = 'Nexus Automator';
const APP_VERSION = app.getVersion();

// Shared path directory for compatibility with python backend
const SHARED_APP_DATA_NAME = 'YousMind AI';

// ================= DYNAMIC API SECURITY TOKEN =================
const crypto = require('crypto');
const apiToken = crypto.randomBytes(32).toString('hex');

// [SECURITY] Always write the token file to ensure PyArmor backend can read it reliably.
try {
    const appDataDir = process.env.APPDATA || require('os').homedir();
    const tokenDir = path.join(appDataDir, SHARED_APP_DATA_NAME);
    const tokenFile = path.join(tokenDir, '.api_token');
    if (!fs.existsSync(tokenDir)) {
        fs.mkdirSync(tokenDir, { recursive: true });
    }
    fs.writeFileSync(tokenFile, apiToken, 'utf8');
    console.log('[Token] API token written to file');
} catch (e) {
    console.error('[Token] Failed to write API token file:', e);
}

function cleanupTokenFile() {
    try {
        const appDataDir = process.env.APPDATA || require('os').homedir();
        const tokenFile = path.join(appDataDir, SHARED_APP_DATA_NAME, '.api_token');
        if (fs.existsSync(tokenFile)) {
            fs.unlinkSync(tokenFile);
            console.log('[Token] Cleaned up API token file on exit');
        }
    } catch (e) {}
}


// State
let mainWindow = null;
let splashWindow = null;
let pythonProcess = null;
let isQuitting = false;
let cachedPCIdentity = null;

// Check if a hardware ID value is generic/weak
function isWeakHardwareValue(val) {
    if (!val) return true;
    const clean = val.replace(/[^a-zA-Z0-9]/g, '').toLowerCase().trim();
    const weakCleaned = new Set([
        'unknown', 'defaultstring', 'tobefilledbyoem', 'none', 'na', 
        'notavailable', 'notapplicable', 'systemmanufacturer', 
        'systemproductname', 'default', 'chassisserialnumber', 
        'bsn12345678901234567', 'error', 'undefined', 'null', 'void'
    ]);
    return weakCleaned.has(clean) || clean.length < 4;
}

// Check if cached PC identity is invalid or weak and lacks a fallback ID
function isPCIdentityInvalidOrWeak(id) {
    if (!id) return true;
    if (!id.includes('NEXTJS_')) return true;
    if (id.includes('CPU_UNKNOWN') || id.includes('MB_UNKNOWN') || id.includes('UNKNOWN')) {
        if (!id.includes('_FID_')) {
            return true;
        }
        return false;
    }
    
    // If _MB_ is missing, it is old/invalid and MUST have _FID_
    if (!id.includes('_MB_')) {
        if (!id.includes('_FID_')) {
            return true;
        }
        return false;
    }
    
    // Split by _MB_
    const parts = id.split('_MB_');
    if (parts.length !== 2) return true;
    
    const cpuPart = parts[0].replace('NEXTJS_CPU_', '');
    let mbPart = parts[1];
    let hasFid = false;
    if (mbPart.includes('_FID_')) {
        hasFid = true;
        mbPart = mbPart.split('_FID_')[0];
    }
    
    const cpuWeak = isWeakHardwareValue(cpuPart);
    const mbWeak = isWeakHardwareValue(mbPart);
    
    // If either CPU or MB is weak, it MUST have a _FID_ suffix
    if ((cpuWeak || mbWeak) && !hasFid) {
        return true;
    }
    
    return false;
}

function getOrCreateMachineId() {
    try {
        const appDataDir = process.env.APPDATA || require('os').homedir();
        const machineIdDir = path.join(appDataDir, SHARED_APP_DATA_NAME);
        const machineIdFile = path.join(machineIdDir, '.machine_id');

        if (!fs.existsSync(machineIdDir)) {
            fs.mkdirSync(machineIdDir, { recursive: true });
        }

        if (fs.existsSync(machineIdFile)) {
            const mid = fs.readFileSync(machineIdFile, 'utf8').trim();
            if (mid) return mid;
        }

        const crypto = require('crypto');
        const mid = crypto.randomBytes(8).toString('hex').toUpperCase();
        fs.writeFileSync(machineIdFile, mid);
        console.log(`[HWID] Created new fallback machine ID: ${mid}`);
        return mid;
    } catch (e) {
        console.error('[HWID] Error creating machine ID:', e);
        const crypto = require('crypto');
        return crypto.randomBytes(8).toString('hex').toUpperCase();
    }
}

function getPCIdentity() {
    return new Promise((resolve) => {
        if (cachedPCIdentity) return resolve(cachedPCIdentity);

        const appDataDir = process.env.APPDATA || require('os').homedir();
        const idDir = path.join(appDataDir, SHARED_APP_DATA_NAME);
        const idFile = path.join(idDir, '.pc_identity');

        try {
            if (fs.existsSync(idFile)) {
                const saved = fs.readFileSync(idFile, 'utf8').trim();
                if (saved && !isPCIdentityInvalidOrWeak(saved)) {
                    cachedPCIdentity = saved;
                    console.log(`[PC Identity] ${cachedPCIdentity} (from cache)`);
                    return resolve(cachedPCIdentity);
                }
            }
        } catch (e) { }

        const execCmd = (cmd) => new Promise((res) => {
            exec(cmd, { timeout: 15000 }, (err, stdout) => {
                if (err) return res('UNKNOWN');
                const lines = stdout.trim().split('\n').filter(l => l.trim());
                const val = lines.length > 0 ? lines[lines.length - 1].trim() : 'UNKNOWN';
                res(val || 'UNKNOWN');
            });
        });

        console.log('[HWID] Generating new PC Identity...');
        const startTime = Date.now();
        const wmiTimeout = new Promise((res) => setTimeout(() => res(['UNKNOWN', 'UNKNOWN']), 18000));

        Promise.race([
            Promise.all([
                execCmd('powershell -NoProfile -Command "(Get-CimInstance Win32_Processor).ProcessorId"'),
                execCmd('powershell -NoProfile -Command "(Get-CimInstance Win32_BaseBoard).SerialNumber"')
            ]),
            wmiTimeout
        ]).then(([cpuId, mbId]) => {
            const cleanCpu = cpuId.replace(/\r/g, '').replace(/\n/g, '').replace(/\s/g, '');
            const cleanMb = mbId.replace(/\r/g, '').replace(/\n/g, '').replace(/\s/g, '');

            const cpuWeak = isWeakHardwareValue(cleanCpu);
            const mbWeak = isWeakHardwareValue(cleanMb);

            if (cpuWeak || mbWeak) {
                const fallbackId = getOrCreateMachineId();
                cachedPCIdentity = `NEXTJS_CPU_${cleanCpu}_MB_${cleanMb}_FID_${fallbackId}`;
            } else {
                cachedPCIdentity = `NEXTJS_CPU_${cleanCpu}_MB_${cleanMb}`;
            }

            console.log(`[PC Identity] ${cachedPCIdentity} (Generated in ${Date.now() - startTime}ms)`);

            try {
                if (!fs.existsSync(idDir)) fs.mkdirSync(idDir, { recursive: true });
                fs.writeFileSync(idFile, cachedPCIdentity);
            } catch (e) { }

            resolve(cachedPCIdentity);
        }).catch(err => {
            console.error('[HWID] Critical failure:', err);
            const fallbackId = getOrCreateMachineId();
            const fallback = `NEXTJS_CPU_UNKNOWN_MB_UNKNOWN_FID_${fallbackId}`;
            resolve(fallback);
        });
    });
}

const isDev = !app.isPackaged;
const resourcesPath = isDev ? path.join(__dirname, '..') : path.join(__dirname, '..');

const BACKEND_PORT = 5000;
const FRONTEND_PORT = 5173;

// Create splash screen window
function createSplashWindow() {
    splashWindow = new BrowserWindow({
        width: 420,
        height: 340,
        transparent: false,
        frame: false,
        alwaysOnTop: true,
        resizable: false,
        skipTaskbar: true,
        show: false,
        icon: path.join(__dirname, '..', 'assets', 'logo.ico'),
        webPreferences: {
            nodeIntegration: false,
            contextIsolation: true
        }
    });

    splashWindow.loadFile(path.join(__dirname, 'splash.html'));
    splashWindow.once('ready-to-show', () => {
        splashWindow.show();
        splashWindow.center();
    });

    splashWindow.on('closed', () => {
        splashWindow = null;
    });
}

function checkBackendReady() {
    return new Promise((resolve) => {
        const req = http.get(`http://localhost:${BACKEND_PORT}/api/health`, (res) => {
            resolve(res.statusCode >= 200 && res.statusCode < 500);
        });
        req.on('error', () => resolve(false));
        req.setTimeout(1000, () => {
            req.destroy();
            resolve(false);
        });
    });
}

async function waitForBackend(maxAttempts = 60) {
    for (let i = 0; i < maxAttempts; i++) {
        const ready = await checkBackendReady();
        if (ready) {
            console.log('✅ Backend is ready!');
            return true;
        }
        console.log(`⏳ Waiting for backend... (${i + 1}/${maxAttempts})`);
        await new Promise(r => setTimeout(r, 1000));
    }
    return false;
}

function startPythonBackend() {
    return new Promise((resolve, reject) => {
        let pythonPath, serverPath, cwd;

        if (isDev) {
            pythonPath = 'python';
            serverPath = path.join(resourcesPath, '..', 'canva_backend', 'canva_server.py');
            cwd = path.join(resourcesPath, '..');
        } else {
            const backendPath = path.join(process.resourcesPath, 'backend');
            pythonPath = path.join(backendPath, 'server.exe');
            serverPath = null;
            cwd = backendPath;
        }

        console.log(`🚀 Starting Python backend at path: ${pythonPath}`);

        const args = serverPath ? [serverPath] : [];
        const envVars = {
            ...process.env,
            PYTHONUNBUFFERED: '1',
            PYTHONIOENCODING: 'utf-8',
            PYTHONUTF8: '1',
            NEXUS_API_TOKEN: apiToken
        };

        pythonProcess = spawn(pythonPath, args, {
            cwd: cwd,
            stdio: ['pipe', 'pipe', 'pipe'],
            env: envVars,
            shell: false,
            windowsHide: true
        });

        const logDir = path.join(app.getPath('userData'), 'logs');
        if (!fs.existsSync(logDir)) fs.mkdirSync(logDir, { recursive: true });
        const logPath = path.join(logDir, 'backend.log');
        const logStream = fs.createWriteStream(logPath, { flags: 'a' });

        logStream.write(`\n--- [${new Date().toISOString()}] Starting Backend ---\n`);

        pythonProcess.stdout.on('data', (data) => {
            const msg = data.toString();
            console.log(`[Python] ${msg}`);
            logStream.write(`[STDOUT] ${msg}`);
        });
        pythonProcess.stderr.on('data', (data) => {
            const msg = data.toString();
            console.error(`[Python ERR] ${msg}`);
            logStream.write(`[STDERR] ${msg}`);
        });

        pythonProcess.on('error', (error) => {
            console.error('Failed to start Python backend:', error);
            logStream.write(`[ERROR] Failed to start: ${error.message}\n`);
            reject(error);
        });

        pythonProcess.on('close', (code, signal) => {
            console.log(`Python exited with code ${code}`);
            if (!isQuitting && code !== 0) {
                dialog.showErrorBox('Backend Error', `Backend stopped unexpectedly.\nExit code: ${code}`);
                app.quit();
            }
        });

        resolve(true);
    });
}

function stopPythonBackend() {
    return new Promise((resolve) => {
        let resolved = false;
        const safeResolve = () => {
            if (!resolved) {
                resolved = true;
                resolve();
            }
        };

        // Send shutdown request to the python backend
        try {
            const req = http.request({
                hostname: '127.0.0.1',
                port: BACKEND_PORT,
                path: '/api/shutdown',
                method: 'POST',
                headers: {
                    'X-API-Token': apiToken
                },
                timeout: 800
            }, (res) => {
                console.log('[Stop] Shutdown request sent to backend, status:', res.statusCode);
                safeResolve();
            });
            req.on('error', (err) => {
                console.log('[Stop] Shutdown request failed (backend might not be running):', err.message);
                safeResolve();
            });
            req.setTimeout(800, () => {
                req.destroy();
                safeResolve();
            });
            req.end();
        } catch (e) {
            console.error('[Stop] Failed to trigger shutdown API:', e);
            safeResolve();
        }

        if (pythonProcess) {
            console.log('🛑 Stopping Python backend...');
            isQuitting = true;
            const pid = pythonProcess.pid;
            if (process.platform === 'win32') {
                try { pythonProcess.kill('SIGTERM'); } catch (e) { }
                try {
                    exec(`taskkill /pid ${pid} /T /F`, (err) => {
                        if (err) console.log(`[Stop] taskkill fallback note: ${err.message}`);
                        safeResolve();
                    });
                } catch (e) {
                    safeResolve();
                }
            } else {
                pythonProcess.kill('SIGTERM');
                safeResolve();
            }
            pythonProcess = null;
        } else {
            // Give the HTTP request a brief moment to transmit in dev mode
            setTimeout(safeResolve, 850);
        }
    });
}

function createMainWindow() {
    mainWindow = new BrowserWindow({
        width: 1280,
        height: 800,
        minWidth: 1024,
        minHeight: 768,
        show: false,
        title: APP_NAME,
        icon: path.join(__dirname, '..', 'assets', 'logo.ico'),
        webPreferences: {
            nodeIntegration: false,
            contextIsolation: true,
            webSecurity: true, // [SECURITY] Re-enabled — was false, which disabled CORS entirely
            preload: path.join(__dirname, 'preload.cjs')
        }
    });

    // [SECURITY] Inject API token at session level — token never reaches renderer JS
    mainWindow.webContents.session.webRequest.onBeforeSendHeaders(
        { urls: ['http://127.0.0.1:5000/*', 'http://localhost:5000/*'] },
        (details, callback) => {
            details.requestHeaders['X-API-Token'] = apiToken;
            callback({ requestHeaders: details.requestHeaders });
        }
    );

    if (isDev) {
        mainWindow.loadURL(`http://localhost:${FRONTEND_PORT}`);
    } else {
        const frontendPath = path.join(resourcesPath, 'dist', 'index.html');
        mainWindow.loadFile(frontendPath);
    }

    mainWindow.once('ready-to-show', () => {
        // Dismiss splash screen
        if (splashWindow) {
            splashWindow.close();
            splashWindow = null;
        }
        mainWindow.show();
        mainWindow.focus();

        // [SECURITY] Block DevTools in production to prevent console-based license bypass
        if (!isDev) {
            mainWindow.webContents.on('devtools-opened', () => {
                mainWindow.webContents.closeDevTools();
            });
        }
    });

    mainWindow.on('closed', () => {
        mainWindow = null;
    });

    mainWindow.webContents.setWindowOpenHandler(({ url }) => {
        shell.openExternal(url);
        return { action: 'deny' };
    });

    const { MenuItem } = require('electron');
    mainWindow.webContents.on('context-menu', (event, params) => {
        const menu = new Menu();
        if (params.isEditable) {
            menu.append(new MenuItem({ role: 'undo' }));
            menu.append(new MenuItem({ role: 'redo' }));
            menu.append(new MenuItem({ type: 'separator' }));
            menu.append(new MenuItem({ role: 'cut' }));
            menu.append(new MenuItem({ role: 'copy' }));
            menu.append(new MenuItem({ role: 'paste' }));
            menu.append(new MenuItem({ type: 'separator' }));
            menu.append(new MenuItem({ role: 'selectAll' }));
        } else if (params.selectionText) {
            menu.append(new MenuItem({ role: 'copy' }));
            menu.append(new MenuItem({ type: 'separator' }));
            menu.append(new MenuItem({ role: 'selectAll' }));
        } else {
            return;
        }
        menu.popup({ window: mainWindow, x: params.x, y: params.y });
    });
}

let isManualCheck = false;

function setupAutoUpdater() {
    // In dev mode, we register the check-for-updates IPC to allow simulations,
    // but we don't start the real autoUpdater checks.
    if (isDev) {
        ipcMain.on('check-for-updates', () => {
            console.log('[Updater] Dev mode manual update check requested.');
            dialog.showMessageBox(mainWindow, {
                type: 'info',
                title: 'Updater (Dev Mode)',
                message: 'Checking for updates... (Simulation)',
                detail: 'Would you like to simulate finding an update or being up-to-date?',
                buttons: ['Simulate: Update Available', 'Simulate: Up to Date', 'Cancel'],
                defaultId: 0,
                cancelId: 2
            }).then((result) => {
                if (result.response === 0) {
                    // Simulate update available
                    mainWindow?.webContents.send('update-available', { version: '2.0.0' });
                    // Trigger download progress simulations
                    setTimeout(() => mainWindow?.webContents.send('update-download-progress', { percent: 25 }), 1000);
                    setTimeout(() => mainWindow?.webContents.send('update-download-progress', { percent: 70 }), 2000);
                    setTimeout(() => {
                        mainWindow?.webContents.send('update-download-progress', { percent: 100 });
                        mainWindow?.webContents.send('update-downloaded', { version: '2.0.0' });
                        
                        dialog.showMessageBox(mainWindow, {
                            type: 'info',
                            title: 'Update Ready (Simulation)',
                            message: 'A new version (2.0.0) of Nexus Automator is ready to install.',
                            detail: 'Would you like to restart the application and apply the update now?',
                            buttons: ['Restart and Install', 'Later'],
                            defaultId: 0,
                            cancelId: 1
                        }).then((res) => {
                            if (res.response === 0) {
                                console.log('[Updater] Simulating app restart for update...');
                                dialog.showMessageBox(mainWindow, {
                                    message: 'App would now quit and install update.'
                                });
                            }
                        });
                    }, 3500);
                } else if (result.response === 1) {
                    // Simulate up to date
                    dialog.showMessageBox(mainWindow, {
                        type: 'info',
                        title: 'No Updates Found',
                        message: 'You are on the latest version!',
                        detail: `Nexus Automator v${APP_VERSION} is up to date.`,
                        buttons: ['OK']
                    });
                }
            });
        });
        return;
    }
    
    // Enable auto-downloading of updates in the background
    autoUpdater.autoDownload = true;
    autoUpdater.logger = console;

    console.log('[Updater] Initializing auto-updater...');

    autoUpdater.on('checking-for-update', () => {
        console.log('[Updater] Checking for update...');
    });
    
    autoUpdater.on('update-available', (info) => {
        console.log('[Updater] Update available:', info.version);
        if (isManualCheck) {
            dialog.showMessageBox(mainWindow, {
                type: 'info',
                title: 'Update Available',
                message: `A new version (${info.version}) was found!`,
                detail: 'Downloading the update in the background. We will notify you when it is ready to install.',
                buttons: ['OK']
            });
        }
        mainWindow?.webContents.send('update-available', info);
    });
    
    autoUpdater.on('update-not-available', (info) => {
        console.log('[Updater] Update not available.');
        if (isManualCheck) {
            dialog.showMessageBox(mainWindow, {
                type: 'info',
                title: 'No Updates Found',
                message: 'You are on the latest version!',
                detail: `Nexus Automator v${APP_VERSION} is up to date.`,
                buttons: ['OK']
            });
            isManualCheck = false;
        }
    });

    autoUpdater.on('download-progress', (progress) => {
        mainWindow?.webContents.send('update-download-progress', progress);
    });
    
    autoUpdater.on('update-downloaded', (info) => {
        console.log('[Updater] Update downloaded:', info.version);
        isManualCheck = false;
        mainWindow?.webContents.send('update-downloaded', info);

        // Notify user using a clean native dialog
        dialog.showMessageBox(mainWindow, {
            type: 'info',
            title: 'Update Ready',
            message: `A new version (${info.version}) of Nexus Automator is ready to install.`,
            detail: 'Would you like to restart the application and apply the update now?',
            buttons: ['Restart and Install', 'Later'],
            defaultId: 0,
            cancelId: 1
        }).then((result) => {
            if (result.response === 0) {
                console.log('[Updater] User chose to restart and install');
                isQuitting = true;
                autoUpdater.quitAndInstall();
            }
        });
    });
    
    autoUpdater.on('error', (error) => {
        console.error('[Updater] Error:', error);
        if (isManualCheck) {
            dialog.showErrorBox('Update Check Failed', `An error occurred while checking for updates:\n${error.message}`);
            isManualCheck = false;
        }
        mainWindow?.webContents.send('update-error', { message: error.message });
    });

    ipcMain.on('check-for-updates', () => {
        console.log('[Updater] Manual update check requested.');
        isManualCheck = true;
        autoUpdater.checkForUpdates().catch(err => {
            console.error('[Updater] Manual check error:', err);
            dialog.showErrorBox('Update Check Failed', `Could not reach update server: ${err.message}`);
            isManualCheck = false;
        });
    });

    ipcMain.on('start-update-download', () => {
        console.log('[Updater] Starting download manually...');
        autoUpdater.downloadUpdate();
    });
    
    ipcMain.on('install-update', () => {
        console.log('[Updater] Quitting and installing via IPC request...');
        isQuitting = true;
        autoUpdater.quitAndInstall();
    });

    // Delay the first update check by 5 seconds to ensure React frontend is fully loaded and listening
    setTimeout(() => {
        autoUpdater.checkForUpdates().catch(err => console.error('[Updater] Auto check error:', err));
    }, 5000);

    setInterval(() => {
        autoUpdater.checkForUpdates();
    }, 4 * 60 * 60 * 1000);
}

app.whenReady().then(async () => {
    createSplashWindow();
    createMainWindow();
    setupAutoUpdater();
    await getPCIdentity();

    if (!isDev) {
        try {
            await startPythonBackend();
            await waitForBackend();
        } catch (e) {
            console.error(e);
        }
    } else {
        await waitForBackend(10);
    }
});

let isShutdownFinished = false;

app.on('window-all-closed', async () => {
    cleanupTokenFile();
    if (process.platform !== 'darwin') {
        if (!isShutdownFinished) {
            await stopPythonBackend();
            isShutdownFinished = true;
        }
        app.quit();
    }
});

app.on('before-quit', async (event) => {
    cleanupTokenFile();
    isQuitting = true;
    if (!isShutdownFinished) {
        event.preventDefault();
        await stopPythonBackend();
        isShutdownFinished = true;
        app.quit();
    }
});

app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createMainWindow();
});

ipcMain.handle('get-app-version', () => APP_VERSION);
ipcMain.handle('get-app-name', () => APP_NAME);
ipcMain.handle('is-dev', () => isDev);
ipcMain.handle('get-pc-identity', () => getPCIdentity());
// [SECURITY] get-api-token IPC handler removed — token injected via session headers instead

ipcMain.on('refocus-window', () => {
    if (mainWindow) {
        mainWindow.blur();
        setTimeout(() => mainWindow.focus(), 50);
    }
});

ipcMain.handle('select-folder', async (event, defaultPath) => {
    const { filePaths } = await dialog.showOpenDialog(mainWindow, {
        properties: ['openDirectory'],
        defaultPath: defaultPath
    });
    return filePaths[0];
});

ipcMain.handle('select-file', async (event, filters) => {
    const { filePaths } = await dialog.showOpenDialog(mainWindow, {
        properties: ['openFile'],
        filters: filters || [
            { name: 'Text Files', extensions: ['txt'] },
            { name: 'All Files', extensions: ['*'] }
        ]
    });
    return filePaths[0];
});

ipcMain.handle('open-folder', async (event, folderPath) => {
    if (fs.existsSync(folderPath)) {
        shell.openPath(folderPath);
        return true;
    }
    return false;
});
