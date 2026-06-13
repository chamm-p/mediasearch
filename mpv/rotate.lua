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
-- Umschaltbar in Stufen: aus -> leicht -> mittel -> stark -> aus.
-- colorbalance hebt Blau an und senkt Rot/Gruen (v.a. Mitten), neutralisiert
-- so den warmen/gelben Farbstich. Eigenes Filter-Label '@wb', damit die
-- Rotation (eine Property, kein Filter) unberuehrt bleibt.
local wb_levels = {
    {name = "aus",    vf = nil},
    {name = "leicht", vf = "lavfi=[colorbalance=rm=-0.04:gm=-0.04:bm=0.12]"},
    {name = "mittel", vf = "lavfi=[colorbalance=rm=-0.08:gm=-0.06:bm=0.22]"},
    {name = "stark",  vf = "lavfi=[colorbalance=rm=-0.12:gm=-0.10:bm=0.32]"},
}
local wb_idx = 1

local function apply_wb()
    mp.command("no-osd vf remove @wb")   -- alte Korrektur entfernen (falls da)
    local lvl = wb_levels[wb_idx]
    if lvl.vf then
        mp.command("no-osd vf add @wb:" .. lvl.vf)
    end
    mp.osd_message("Weissabgleich: " .. lvl.name, 1.5)
end

mp.add_forced_key_binding("w", "wb-cycle", function()
    wb_idx = wb_idx % #wb_levels + 1
    apply_wb()
end)
