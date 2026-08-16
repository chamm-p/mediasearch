-- mediasearch mpv-Script: Hotkeys fuer Video-Rotation + Weissabgleich
-- Wird via --script=... beim mpv-Start geladen.
-- mpv ruft on_load() automatisch auf wenn das Script geladen ist.

mp.osd_message("mediasearch: b/n drehen, B reset, w Weissabgleich, "
               .. "Mausrad spult (Shift = fein), ESC beendet", 3)

local function rotate_by(delta)
    local cur = mp.get_property_number("video-rotate", 0) or 0
    local new = (cur + delta) % 360
    if new < 0 then new = new + 360 end
    mp.set_property("video-rotate", new)
    mp.osd_message("Rotation: " .. new .. " Grad", 1.5)
end

mp.add_forced_key_binding("b", "rotate-cw",  function() rotate_by(90)  end)
mp.add_forced_key_binding("n", "rotate-ccw", function() rotate_by(-90) end)
mp.add_forced_key_binding("B", "rotate-reset", function()
    mp.set_property("video-rotate", 0)
    mp.osd_message("Rotation: 0 Grad", 1.5)
end)

-- ---- ESC beendet den Player (nicht nur Fullscreen verlassen) ----
-- mpv-Default: ESC = Fullscreen aus. Hier auf 'quit' gelegt, passend zur
-- Bedienung "V/Klick startet, ESC beendet" (und zur Nutzungsdauer-Messung).
-- q bleibt wie gewohnt zusaetzlich erhalten.
mp.add_forced_key_binding("ESC", "quit-on-esc", function()
    mp.commandv("quit")
end)

-- ---- Mausrad = SPULEN statt Lautstaerke ----
-- mpv legt das Rad per Default auf volume up/down. Hier umgebogen:
--   Rad hoch/runter        = 5s vor/zurueck
--   Shift + Rad            = 1s (Feinsprung)
-- 'exact' sucht framegenau statt nur zum Keyframe.
local function seek_by(sec)
    mp.commandv("seek", tostring(sec), "relative", "exact")
end

local function bind_wheel(key, name, sec)
    -- add_forced_key_binding ueberschreibt die Default-Bindung (volume).
    mp.add_forced_key_binding(key, name, function() seek_by(sec) end)
end

bind_wheel("WHEEL_UP",         "seek-fwd",        5)
bind_wheel("WHEEL_DOWN",       "seek-back",      -5)
bind_wheel("Shift+WHEEL_UP",   "seek-fwd-fine",   1)
bind_wheel("Shift+WHEEL_DOWN", "seek-back-fine", -1)
-- Aeltere mpv-Versionen/Backends melden das Rad als MOUSE_BTN3/4:
bind_wheel("MOUSE_BTN3",       "seek-fwd-legacy",   5)
bind_wheel("MOUSE_BTN4",       "seek-back-legacy", -5)

-- ---- Weissabgleich gegen Gelbstich (Taste w) ----
-- Feste Korrektur auf der GPU (glsl-shader) statt CPU-Filter: laeuft auch bei
-- 4K fluessig, weil die Hardware-Dekodierung erhalten bleibt (ein CPU-Filter
-- wuerde jedes Frame ueber die CPU ziehen -> Ruckeln bei hoher Aufloesung).
-- Weissabgleich gegen Gelbstich. Default 'mittel' (per config.toml
-- [media] video_wb aenderbar: aus | mittel | stark). Taste w schaltet
-- zyklisch weiter. Die Shader senken Rot leicht und heben Blau
-- (luminanz-erhaltend) und neutralisieren so den warmen Gelbstich.
--
-- WICHTIG (Swapchain-Problem): das Setzen von glsl-shaders zwingt mpv, die
-- Render-Pipeline neu aufzubauen. Passiert das ZU FRUEH (beim Script-Laden,
-- bevor der Video-Ausgang steht), scheitert auf manchen GPUs/Treibern die
-- Swapchain-Erstellung und mpv beendet sich sofort. Deshalb wird der Default
-- erst angewendet, wenn die Wiedergabe laeuft (file-loaded/playback-restart).
-- wbdir + wbdefault kommen per --script-opts=... vom Server.
local wbdir     = (mp.get_opt and mp.get_opt("wbdir")) or nil
local wbdefault = (mp.get_opt and mp.get_opt("wbdefault")) or "mittel"
local wb_levels = {
    {name = "aus",    file = nil},
    {name = "mittel", file = "wb_mittel.glsl"},
    {name = "stark",  file = "wb_stark.glsl"},
}
local wb_idx = 1                       -- Fallback: aus
for i, l in ipairs(wb_levels) do
    if l.name == wbdefault then wb_idx = i end
end

local function apply_wb(silent)
    local lvl = wb_levels[wb_idx]
    -- defensiv: ein glsl-shaders-Fehler (vo ohne Shader-Support etc.) darf
    -- weder das Script abbrechen noch die Wiedergabe stoeren.
    pcall(function()
        if lvl.file and wbdir then
            mp.set_property("glsl-shaders", wbdir .. "/" .. lvl.file)
        else
            mp.set_property("glsl-shaders", "")
        end
    end)
    if not silent then
        mp.osd_message("Weissabgleich: " .. lvl.name, 1.5)
    end
end

mp.add_forced_key_binding("w", "wb-cycle", function()
    if not wbdir then
        mp.osd_message("Weissabgleich: Shader-Pfad fehlt (wbdir)", 2)
        return
    end
    wb_idx = wb_idx % #wb_levels + 1
    apply_wb()
end)

-- Default NICHT sofort setzen, sondern erst wenn die Wiedergabe steht.
-- Sonst kann der Swapchain-Neuaufbau beim Start fehlschlagen und mpv
-- beendet sich (schwarzes Blinken). 'playback-restart' feuert, wenn der
-- erste Frame gerendert wurde - ab da ist der Video-Ausgang bereit.
local wb_applied = false
local function apply_default_once()
    if wb_applied or not wbdir then return end
    wb_applied = true
    if wb_levels[wb_idx].file == nil then return end   -- 'aus' -> nichts tun
    -- kleiner Aufschub, damit der vo wirklich fertig initialisiert ist
    mp.add_timeout(0.3, function()
        local ok = pcall(function()
            mp.set_property("glsl-shaders", wbdir .. "/" .. wb_levels[wb_idx].file)
        end)
        if not ok then
            wb_idx = 1   -- auf 'aus' zurueckfallen, Wiedergabe hat Vorrang
            mp.osd_message("Weissabgleich konnte nicht aktiviert werden", 2)
        end
    end)
end
mp.register_event("playback-restart", apply_default_once)
mp.register_event("file-loaded", apply_default_once)
