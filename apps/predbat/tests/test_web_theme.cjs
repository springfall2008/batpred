// Behaviour tests for the generated theme scripts, using only Node.js built-ins.
// Run directly: node apps/predbat/tests/test_web_theme.cjs [python executable]
const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const path = require('node:path');
const vm = require('node:vm');

const header = execFileSync(process.argv[2] || 'python3', ['-c',
    "from web_helper import get_header_html; print(get_header_html('Test', False, './dash', [], 'v1.0', ''))"
], { cwd: path.resolve(__dirname, '..'), encoding: 'utf8' });
const scripts = [...header.matchAll(/<script>([\s\S]*?)<\/script>/g)]
    .map(match => match[1]).filter(script => /function (getDarkModePreference|applyDarkMode)/.test(script));
assert.equal(scripts.length, 2);

function classList() {
    const values = new Set();
    return { add: value => values.add(value), remove: value => values.delete(value), contains: value => values.has(value) };
}

function page(stored, dark, legacy = false, mediaAvailable = true) {
    const storage = new Map(stored === null ? [] : [['darkMode', stored]]);
    const selector = { value: 'system' };
    const logo = { src: '', getAttribute: name => name === 'data-dark-src' ? 'dark.png' : 'light.png' };
    let listener;
    let reloads = 0;
    const media = { matches: dark };
    media[legacy ? 'addListener' : 'addEventListener'] = (...args) => { listener = args.at(-1); };
    const document = {
        documentElement: { classList: classList() }, body: { classList: classList() },
        addEventListener: () => {},
        getElementById: id => id === 'theme-mode' ? selector : id === 'logo-image' ? logo : null
    };
    const window = mediaAvailable ? { matchMedia: () => media } : {};
    const context = vm.createContext({ document, window,
        localStorage: { getItem: key => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, String(value)), removeItem: key => storage.delete(key) },
        location: { reload: () => { reloads++; } }
    });
    scripts.forEach(script => vm.runInContext(script, context));
    const preCssDark = document.documentElement.classList.contains('dark-mode');
    window.onload();
    return { context, document, storage, selector, logo, preCssDark, reloads: () => reloads,
        osChange: value => { media.matches = value; listener(); } };
}

function checkAppearance(page, dark, mode) {
    assert.equal(page.document.documentElement.classList.contains('dark-mode'), dark);
    assert.equal(page.document.body.classList.contains('dark-mode'), dark);
    assert.equal(page.logo.src, dark ? 'dark.png' : 'light.png');
    assert.equal(page.selector.value, mode);
}

for (const legacy of [false, true]) {
    for (const stored of [null, 'true', 'false']) {
        for (const osDark of [false, true]) {
            const current = page(stored, osDark, legacy);
            const mode = stored === null ? 'system' : stored === 'true' ? 'dark' : 'light';
            const expected = stored === null ? osDark : stored === 'true';
            assert.equal(current.preCssDark, expected);
            checkAppearance(current, expected, mode);
            current.osChange(!osDark);
            checkAppearance(current, stored === null ? !osDark : expected, mode);
            assert.equal(current.storage.get('darkMode') ?? null, stored);
            assert.equal(current.reloads(), 0);

            for (const choice of ['light', 'dark', 'system']) {
                current.context.setThemeMode(choice);
                const saved = current.storage.get('darkMode') ?? null;
                assert.equal(saved, choice === 'system' ? null : choice === 'dark' ? 'true' : 'false');
                const reloaded = page(saved, osDark, legacy);
                checkAppearance(reloaded, choice === 'system' ? osDark : choice === 'dark', choice);
                reloaded.osChange(!osDark);
                checkAppearance(reloaded, choice === 'system' ? !osDark : choice === 'dark', choice);
            }
            assert.equal(current.reloads(), 3);
        }
    }
}
checkAppearance(page(null, false, false, false), false, 'system');
checkAppearance(page('true', false, false, false), true, 'dark');
console.log('PASS: theme choices, persistence, pre-CSS appearance, logo, live OS changes, legacy listener and missing matchMedia');
