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
-- Umschaltbar: aus -> auto -> manuell -> aus.
--   auto    = greyedge: schaetzt das Falschlicht selbst und korrigiert es
--             (echter Weissabgleich, nutzt z.B. das weisse Kleid als Referenz)
--   manuell = hautfreundlich: Gruen runter (Richtung Magenta/rosa) + Blau hoch,
--             Rot bleibt -> Haut wird rosa statt gelb-gruen, ohne alles blau
--             zu kippen wie ein reiner colorbalance-Blaushift.
-- Eigenes Filter-Label '@wb', damit die Rotation (Property, kein Filter)
-- unberuehrt bleibt. Bei fehlendem Filter zeigt mpv nur eine OSD-Meldung.
local wb_levels = {
    {name = "aus",     vf = nil},
    {name = "auto",    vf = "lavfi=[greyedge=difford=1:minknorm=1:sigma=2]"},
    {name = "manuell", vf = "lavfi=[colorbalance=rm=0.04:gm=-0.10:bm=0.14:rh=0.03:gh=-0.06:bh=0.08]"},
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
