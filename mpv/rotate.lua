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

-- ====================================================================
-- Video-Abspielmodus (Taste V im Browser): Slideshow fuer Videos.
-- Nur aktiv, wenn der Server vs_sid/vs_url mitgibt (sonst - z.B. beim
-- einzelnen Video aus dem Dateimanager - bleiben alle mpv-Tasten normal).
--   vs_next (Default c) : naechstes Video
--   vs_fav  (Default f) : Favorit an/aus (bleibt beim Video)
--   vs_prev (Default z) : vorheriges Video
--   vs_unfav (Default F = Shift+f) : Favorit entfernen (bleibt beim Video)
--   vs_copy (Default k) : Video nach [player] copy_dir KOPIEREN (Original bleibt)
--   Favoriten tragen DAUERHAFT einen grossen goldenen Stern oben rechts.
--   q / ESC             : Abspielmodus beenden
-- Jeder Videowechsel wird an den Server gemeldet: dort wird das Video SOFORT
-- als gesehen vermerkt und die Nutzungsdauer des vorigen gebucht.
-- ====================================================================
local vs_sid = mp.get_opt("vs_sid")
local vs_url = mp.get_opt("vs_url")
if vs_sid and vs_sid ~= "" and vs_url and vs_url ~= "" then
    local vs_py   = mp.get_opt("vs_py") or "python3"
    local key_nx  = mp.get_opt("vs_next") or "c"
    local key_fav = mp.get_opt("vs_fav") or "f"
    local key_pv  = mp.get_opt("vs_prev") or "z"
    local key_unf = mp.get_opt("vs_unfav") or "F"
    local key_cp  = mp.get_opt("vs_copy") or "k"
    local utils   = require("mp.utils")
    local favtag  = mp.get_opt("vs_favtag") or "favorit"

    -- HTTP-POST ueber Python (ist auf dem Zielsystem sicher vorhanden, curl
    -- nicht unbedingt). Asynchron: die Wiedergabe wartet nie auf den Server.
    -- Server-Fehler (HTTP 4xx/5xx) -> Antwort-JSON auf stdout, Exitcode 2,
    -- damit mpv die Meldung (z.B. "Zielordner existiert nicht") anzeigen kann.
    local post_code = table.concat({
        "import sys,json,urllib.request,urllib.error",
        "d=json.dumps({'sid':sys.argv[2],'ev':sys.argv[3],'pos':int(sys.argv[4]),'tag':sys.argv[5]}).encode()",
        "r=urllib.request.Request(sys.argv[1],d,{'Content-Type':'application/json'})",
        "try:",
        "    print(urllib.request.urlopen(r,timeout=float(sys.argv[6])).read().decode())",
        "except urllib.error.HTTPError as e:",
        "    print(e.read().decode()); sys.exit(2)",
    }, "\n")

    local function post(ev, pos, on_ok, timeout, on_err)
        mp.command_native_async({
            name = "subprocess",
            args = { vs_py, "-c", post_code, vs_url, vs_sid, ev,
                     tostring(pos), favtag, tostring(timeout or 10) },
            playback_only = false,     -- nicht abbrechen, wenn das Video wechselt
            capture_stdout = true,
            capture_stderr = true,
        }, function(success, res, err)
            local ok = success and res and res.status == 0
            if ok then
                if on_ok then
                    on_ok(utils.parse_json(res.stdout or "") or {})
                end
            else
                mp.msg.warn("vs post " .. ev .. " fehlgeschlagen: "
                            .. tostring(err or (res and ((res.stdout or "") .. (res.stderr or ""))) or "?"))
                local detail = res and utils.parse_json(res.stdout or "")
                detail = (type(detail) == "table" and detail.detail) or "Server nicht erreichbar"
                if on_err then
                    on_err(tostring(detail))
                elseif ev == "fav" or ev == "unfav" or ev == "toggle" then
                    mp.osd_message("Favorit konnte NICHT gespeichert werden", 3)
                end
            end
        end)
    end

    local function pos_count()
        return mp.get_property_number("playlist-pos", 0) or 0,
               mp.get_property_number("playlist-count", 0) or 0
    end

    local function info()
        local pos, cnt = pos_count()
        mp.osd_message(string.format("Video %d / %d", pos + 1, cnt), 2)
    end

    -- Dauerhafter Favoriten-Stern: eigenes OSD-Overlay (bleibt stehen, solange
    -- das Video Favorit ist - anders als osd_message, das nach Sekunden
    -- verschwindet). Gold mit schwarzem Rand, oben rechts, auf hellem wie
    -- dunklem Bild gut sichtbar. ASS-Farben sind BGR: &H00D7FF& = Gold.
    local star = mp.create_osd_overlay and mp.create_osd_overlay("ass-events")
    local function set_star(on)
        if not star then return end      -- sehr altes mpv ohne Overlays
        if on then
            star.data = "{\\an9\\fs64\\b1\\bord4\\shad2"
                     .. "\\1c&H00D7FF&\\3c&H000000&}★ Favorit"
            star:update()
        else
            star:remove()
        end
    end

    local function go_next()
        local pos, cnt = pos_count()
        if pos + 1 >= cnt then
            mp.osd_message("Letztes Video - q/ESC beendet", 3)
            return
        end
        mp.commandv("playlist-next", "force")
    end

    mp.add_forced_key_binding(key_nx, "vs-next", go_next)

    mp.add_forced_key_binding(key_pv, "vs-prev", function()
        local pos = pos_count()
        if pos <= 0 then
            mp.osd_message("Erstes Video", 2)
            return
        end
        mp.commandv("playlist-prev", "force")
    end)

    mp.add_forced_key_binding(key_cp, "vs-copy", function()
        local pos = pos_count()
        local name = mp.get_property("filename") or "Video"
        mp.osd_message("Kopiere " .. name .. " ...", 3)
        -- grosse Videos/Netzlaufwerk: bis zu 1 h warten; die Wiedergabe laeuft
        -- weiter, man kann auch schon zum naechsten Video wechseln
        post("copy", pos, function(r)
            if r.busy then
                mp.osd_message("Kopie laeuft bereits: " .. (r.name or name), 3)
            elseif r.skipped then
                mp.osd_message("Schon kopiert: " .. (r.name or name), 3)
            else
                mp.osd_message("Kopiert ✓  " .. (r.dest or name), 4)
            end
        end, 3600, function(detail)
            mp.osd_message("Kopieren fehlgeschlagen: " .. detail, 6)
        end)
    end)

    mp.add_forced_key_binding(key_unf, "vs-unfav", function()
        local pos = pos_count()
        post("unfav", pos, function(r)
            if pos_count() == pos then set_star(false) end
            if r.changed and r.changed > 0 then
                mp.osd_message("Favorit entfernt", 2)
            else
                mp.osd_message("war kein Favorit", 2)
            end
        end)
    end)

    mp.add_forced_key_binding(key_fav, "vs-fav", function()
        local pos = pos_count()
        -- Server entscheidet anhand der DB (kein veralteter Lua-Zustand)
        post("toggle", pos, function(r)
            if pos_count() == pos then set_star(r.fav) end
            if r.fav then
                mp.osd_message("★ Favorit", 2)
            else
                mp.osd_message("Favorit entfernt", 2)
            end
        end)
    end)

    -- Jeder neu geladene Eintrag -> 'gesehen' + Wechsel melden. Der Server
    -- ignoriert die Meldung fuer das Video, das gerade schon aktuell ist
    -- (Startvideo hat er beim Start selbst vermerkt).
    mp.register_event("file-loaded", function()
        local pos = pos_count()
        post("start", pos, function(r)
            -- Antwort kommt nach ~0.1s; nur anzeigen, wenn noch dasselbe Video
            if pos_count() == pos then set_star(r.fav) end
        end)
        -- manuelle Drehung (b/n) gilt nur fuers jeweilige Video
        mp.set_property_number("video-rotate", 0)
        set_star(false)                  -- neues Video: erst ohne Stern
        info()
    end)

    mp.msg.info(string.format("Video-Abspielmodus: %s=weiter %s=Favorit %s=zurueck "
                              .. "%s=Favorit entfernen %s=kopieren",
                              key_nx, key_fav, key_pv, key_unf, key_cp))
end
