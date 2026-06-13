-- mediasearch mpv-Script: Hotkeys fuer Video-Rotation + Weissabgleich
-- Wird via --script=... beim mpv-Start geladen.
-- mpv ruft on_load() automatisch auf wenn das Script geladen ist.

mp.osd_message("mediasearch: b/n drehen, B reset, w Weissabgleich", 3)

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

-- ---- Weissabgleich gegen Gelbstich (Taste w) ----
-- Feste Korrektur auf der GPU (glsl-shader) statt CPU-Filter: laeuft auch bei
-- 4K fluessig, weil die Hardware-Dekodierung erhalten bleibt (ein CPU-Filter
-- wuerde jedes Frame ueber die CPU ziehen -> Ruckeln bei hoher Aufloesung).
-- Start = mittel (an by default - passt fuer nahezu alles). Taste w schaltet
-- weiter: mittel -> stark -> aus -> mittel ...
-- Die Shader senken Rot leicht und heben Blau (luminanz-erhaltend) und
-- neutralisieren so den warmen Gelbstich.
-- wbdir kommt per --script-opts=wbdir=... vom Server (Pfad zum mpv/-Ordner).
local wbdir = (mp.get_opt and mp.get_opt("wbdir")) or nil
local wb_levels = {
    {name = "mittel", file = "wb_mittel.glsl"},
    {name = "stark",  file = "wb_stark.glsl"},
    {name = "aus",    file = nil},
}
local wb_idx = 1

local function apply_wb(silent)
    local lvl = wb_levels[wb_idx]
    if lvl.file and wbdir then
        mp.set_property("glsl-shaders", wbdir .. "/" .. lvl.file)
    else
        mp.set_property("glsl-shaders", "")
    end
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

-- Default beim Laden setzen: mittel (sofern Shader-Pfad bekannt).
if wbdir then
    apply_wb(true)
end
