import { getContext, extension_settings, saveSettingsDebounced } from '../../../scripts/extensions.js';
import { appendMediaToMessage } from '../../../../script.js';
import { eventSource, event_types } from '../../../../script.js';

const EXT_NAME = 'gemini-image';
const MEDIA_SOURCE_GENERATED = 'generated';
const MEDIA_TYPE_IMAGE = 'image';

const defaultSettings = {
    enabled: false,
    api_url: '',
    api_key: '',
    auto_generate: false,
    prompt_template: 'Based on the following scene, create a detailed image prompt in English:\n\n{{text}}\n\nRespond with ONLY the image generation prompt, nothing else.',
};

function loadSettings() {
    extension_settings[EXT_NAME] = extension_settings[EXT_NAME] || {};
    for (const [key, val] of Object.entries(defaultSettings)) {
        if (extension_settings[EXT_NAME][key] === undefined) {
            extension_settings[EXT_NAME][key] = val;
        }
    }
}

function getSettings() {
    return extension_settings[EXT_NAME];
}

async function generateImage(prompt) {
    const settings = getSettings();
    if (!settings.api_url || !settings.api_key) {
        throw new Error('API URL and Key are required');
    }

    const url = settings.api_url.replace(/\/+$/, '') + '/v1/images/generations';

    const response = await fetch(url, {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
            'Authorization': `Bearer ${settings.api_key}`,
        },
        body: JSON.stringify({
            prompt: prompt,
            model: 'gemini-3.0-flash',
            response_format: 'b64_json',
            n: 1,
        }),
    });

    if (!response.ok) {
        const text = await response.text();
        throw new Error(`Image generation failed: ${response.status} ${text.slice(0, 200)}`);
    }

    const data = await response.json();
    if (!data.data || !data.data[0] || !data.data[0].b64_json) {
        throw new Error('No image data in response');
    }

    return data.data[0].b64_json;
}

function extractSceneFromMessage(text) {
    // Strip HTML tags for scene extraction
    const stripped = text.replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ').trim();
    // Take last 500 chars as the most recent scene context
    return stripped.slice(-500);
}

async function generateAndAttachImage(messageIndex) {
    const context = getContext();
    const message = context.chat[messageIndex];
    if (!message || message.is_user) return;

    const settings = getSettings();
    const statusEl = document.getElementById('gi-status');

    try {
        if (statusEl) {
            statusEl.className = 'gi-status';
            statusEl.textContent = 'Generating image...';
        }

        const sceneText = extractSceneFromMessage(message.mes);
        if (!sceneText || sceneText.length < 10) return;

        // Use the scene text directly as the image prompt
        const imagePrompt = sceneText.slice(0, 300);

        const base64Image = await generateImage(imagePrompt);
        const imageUrl = `data:image/png;base64,${base64Image}`;

        // Attach to message
        if (!message.extra) message.extra = {};
        if (!Array.isArray(message.extra.media)) message.extra.media = [];

        const attachment = {
            url: imageUrl,
            type: MEDIA_TYPE_IMAGE,
            title: imagePrompt.slice(0, 100),
            source: MEDIA_SOURCE_GENERATED,
        };

        message.extra.inline_image = !(message.extra.media.length && !message.extra.inline_image);
        message.extra.media.push(attachment);
        message.extra.media_index = message.extra.media.length - 1;

        const messageElement = $(`.mes[mesid="${messageIndex}"]`);
        if (messageElement.length) {
            appendMediaToMessage(message, messageElement);
        }

        await context.saveChat();

        if (statusEl) {
            statusEl.className = 'gi-status ok';
            statusEl.textContent = 'Image generated!';
            setTimeout(() => { statusEl.textContent = ''; }, 3000);
        }
    } catch (e) {
        console.error('[gemini-image]', e);
        if (statusEl) {
            statusEl.className = 'gi-status err';
            statusEl.textContent = e.message;
        }
    }
}

async function onMessageReceived(messageIndex) {
    const settings = getSettings();
    if (!settings.enabled || !settings.auto_generate) return;
    if (!settings.api_url || !settings.api_key) return;

    // Small delay to let the message render
    await new Promise(r => setTimeout(r, 500));
    await generateAndAttachImage(messageIndex);
}

function onSettingsHtml() {
    const settings = getSettings();
    const html = `
    <div id="gemini-image-settings" class="inline-drawer">
        <div class="inline-drawer-toggle inline-drawer-header">
            <b>Gemini Image Generator</b>
            <div class="inline-drawer-icon fa-solid fa-circle-chevron-down down"></div>
        </div>
        <div class="inline-drawer-content">
            <div class="gi-row">
                <label>Enable</label>
                <input id="gi-enabled" type="checkbox" ${settings.enabled ? 'checked' : ''} />
            </div>
            <div class="gi-row">
                <label>API URL</label>
                <input id="gi-api-url" type="text" class="text_pole" value="${settings.api_url}" placeholder="http://your-server:9800" />
            </div>
            <div class="gi-row">
                <label>API Key</label>
                <input id="gi-api-key" type="password" class="text_pole" value="${settings.api_key}" placeholder="Your API key" />
            </div>
            <div class="gi-row">
                <label>Auto-generate</label>
                <input id="gi-auto" type="checkbox" ${settings.auto_generate ? 'checked' : ''} />
                <small>Generate image after each AI reply</small>
            </div>
            <div class="gi-row">
                <button id="gi-test" class="menu_button">Test Connection</button>
                <button id="gi-manual" class="menu_button">Generate for Last Message</button>
            </div>
            <div id="gi-status" class="gi-status"></div>
        </div>
    </div>`;

    $('#extensions_settings2').append(html);

    $('#gi-enabled').on('change', function () {
        settings.enabled = this.checked;
        saveSettingsDebounced();
    });
    $('#gi-api-url').on('input', function () {
        settings.api_url = this.value.trim();
        saveSettingsDebounced();
    });
    $('#gi-api-key').on('input', function () {
        settings.api_key = this.value.trim();
        saveSettingsDebounced();
    });
    $('#gi-auto').on('change', function () {
        settings.auto_generate = this.checked;
        saveSettingsDebounced();
    });
    $('#gi-test').on('click', async function () {
        const statusEl = document.getElementById('gi-status');
        try {
            statusEl.className = 'gi-status';
            statusEl.textContent = 'Testing...';
            const url = settings.api_url.replace(/\/+$/, '') + '/health';
            const resp = await fetch(url);
            const data = await resp.json();
            statusEl.className = 'gi-status ok';
            statusEl.textContent = `Connected! ${data.available || 0} containers available`;
        } catch (e) {
            statusEl.className = 'gi-status err';
            statusEl.textContent = `Failed: ${e.message}`;
        }
    });
    $('#gi-manual').on('click', async function () {
        const context = getContext();
        const lastIndex = context.chat.length - 1;
        if (lastIndex >= 0) {
            await generateAndAttachImage(lastIndex);
        }
    });
}

jQuery(async () => {
    loadSettings();
    onSettingsHtml();
    eventSource.on(event_types.MESSAGE_RECEIVED, onMessageReceived);
});
