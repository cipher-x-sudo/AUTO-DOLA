'use strict';

/**
 * Lab-only Frida hook for Nexus Automator server.exe (PyInstaller + Python 3.11).
 * Forces local license/session state to "approved" inside the embedded interpreter.
 */

const PY_DLL = 'python311.dll';
const PATCH_INTERVAL_MS = 750;

const INJECT_PY = String.raw`
import sys, time

def _lab_force_logged_in():
    touched = []

    def _allow_all():
        return None

    for name, mod in list(sys.modules.items()):
        if mod is None:
            continue
        has_license = any(
            hasattr(mod, attr)
            for attr in ('license_cache_data', 'license_cache', 'check_license_before_request', 'verify_license_status')
        )
        if not has_license and 'canva_server' not in name:
            continue
        try:
            if hasattr(mod, 'DISABLE_TOKEN_CHECK'):
                mod.DISABLE_TOKEN_CHECK = True

            lcd = getattr(mod, 'license_cache_data', None)
            if isinstance(lcd, dict):
                lcd['approved'] = True
                lcd['last_check_time'] = time.time()
                if not lcd.get('secureToken'):
                    lcd['secureToken'] = 'lab-local-bypass'

            lc = getattr(mod, 'license_cache', None)
            if isinstance(lc, dict):
                if not lc.get('pcId') or lc.get('pcId') == 'Loading...':
                    lc['pcId'] = 'LAB-SESSION'

            state = getattr(mod, 'state', None)
            if isinstance(state, dict):
                nested = state.get('license_cache_data')
                if isinstance(nested, dict):
                    nested['approved'] = True
                    nested['last_check_time'] = time.time()
                    if not nested.get('secureToken'):
                        nested['secureToken'] = 'lab-local-bypass'
                cache = state.get('license_cache')
                if isinstance(cache, dict) and (not cache.get('pcId') or cache.get('pcId') == 'Loading...'):
                    cache['pcId'] = 'LAB-SESSION'

            if not getattr(mod, '_lab_verify_replaced', False) and hasattr(mod, 'verify_license_status'):
                def _stub_verify(*_args, **_kwargs):
                    token = 'lab-local-bypass'
                    pc = 'LAB-SESSION'
                    lcd = getattr(mod, 'license_cache_data', None)
                    lc = getattr(mod, 'license_cache', None)
                    if isinstance(lcd, dict) and lcd.get('secureToken'):
                        token = lcd['secureToken']
                    if isinstance(lc, dict) and lc.get('pcId'):
                        pc = lc['pcId']
                    return {'approved': True, 'secureToken': token, 'pcId': pc}
                mod.verify_license_status = _stub_verify
                mod._lab_verify_replaced = True
                touched.append(name + ':verify')

            if not getattr(mod, '_lab_before_replaced', False) and hasattr(mod, 'check_license_before_request'):
                mod.check_license_before_request = _allow_all
                mod._lab_before_replaced = True
                touched.append(name + ':before')

            app = getattr(mod, 'app', None)
            if app is not None and hasattr(app, 'before_request_funcs'):
                try:
                    for key in list(app.before_request_funcs.keys()):
                        funcs = app.before_request_funcs.get(key) or []
                        app.before_request_funcs[key] = [
                            _allow_all if getattr(fn, '__name__', '') == 'check_license_before_request' else fn
                            for fn in funcs
                        ]
                    touched.append(name + ':flask-before')
                except Exception as exc:
                    touched.append(name + ':flask-before-err:' + str(exc))

            if has_license or 'canva_server' in name:
                touched.append(name)
        except Exception as exc:
            touched.append(name + ':err:' + str(exc))
    return touched

try:
    import requests
    if not getattr(requests, '_lab_patched', False):
        _orig_post = requests.post
        _orig_get = requests.get
        _orig_request = requests.Session.request
        def _fake_license_response():
            class _Resp:
                status_code = 200
                text = '{"approved":true,"secureToken":"lab-local-bypass","pcId":"LAB-SESSION"}'
                def json(self):
                    return {"approved": True, "secureToken": "lab-local-bypass", "pcId": "LAB-SESSION"}
            return _Resp()
        def _lab_post(url, *args, **kwargs):
            if 'yousmind.com' in str(url):
                return _fake_license_response()
            return _orig_post(url, *args, **kwargs)
        def _lab_get(url, *args, **kwargs):
            if 'yousmind.com' in str(url):
                return _fake_license_response()
            return _orig_get(url, *args, **kwargs)
        def _lab_session_request(self, method, url, *args, **kwargs):
            if 'yousmind.com' in str(url):
                return _fake_license_response()
            return _orig_request(self, method, url, *args, **kwargs)
        requests.post = _lab_post
        requests.get = _lab_get
        requests.Session.request = _lab_session_request
        requests._lab_patched = True
        print('[lab-frida] requests shim installed')
except Exception as exc:
    print('[lab-frida] requests shim failed:', exc)

print('[lab-frida] patched modules:', _lab_force_logged_in())
`;

let pythonApi = null;
let patchTimer = null;
let patchCount = 0;

function resolvePythonApi() {
  const mod = Process.findModuleByName(PY_DLL);
  if (!mod) {
    return null;
  }

  try {
    mod.ensureInitialized();
  } catch (e) {
    // Module may already be initialized.
  }

  const exp = (name, ret, args) => {
    const addr = mod.getExportByName(name);
    if (addr === null) {
      throw new Error(`missing export ${name} in ${PY_DLL}`);
    }
    return new NativeFunction(addr, ret, args);
  };

  return {
    PyGILState_Ensure: exp('PyGILState_Ensure', 'int', []),
    PyGILState_Release: exp('PyGILState_Release', 'void', ['int']),
    PyRun_SimpleString: exp('PyRun_SimpleString', 'int', ['pointer']),
  };
}

function runPython(code) {
  if (!pythonApi) {
    pythonApi = resolvePythonApi();
  }
  if (!pythonApi) {
    return false;
  }

  const state = pythonApi.PyGILState_Ensure();
  let ok = false;
  try {
    const codePtr = Memory.allocUtf8String(code);
    const rc = pythonApi.PyRun_SimpleString(codePtr);
    ok = rc === 0;
  } catch (e) {
    console.log('[lab-frida] PyRun_SimpleString failed:', e);
  } finally {
    pythonApi.PyGILState_Release(state);
  }
  return ok;
}

function applyLicenseBypass() {
  const ok = runPython(INJECT_PY);
  patchCount += 1;
  if (ok) {
    console.log(`[lab-frida] license bypass inject #${patchCount} applied`);
  }
  return ok;
}

function startPatchLoop() {
  if (patchTimer !== null) {
    return;
  }
  applyLicenseBypass();
  patchTimer = setInterval(applyLicenseBypass, PATCH_INTERVAL_MS);
}

function waitForPythonAndPatch() {
  const timer = setInterval(() => {
    const mod = Process.findModuleByName(PY_DLL);
    if (!mod) {
      return;
    }
    clearInterval(timer);
    console.log('[lab-frida] python311.dll loaded');
    setTimeout(startPatchLoop, 800);
  }, 100);
}

rpc.exports = {
  patchnow() {
    return applyLicenseBypass();
  },
  stats() {
    return { patchCount, pythonLoaded: !!Process.findModuleByName(PY_DLL) };
  },
};

console.log('[lab-frida] waiting for embedded Python...');
waitForPythonAndPatch();
