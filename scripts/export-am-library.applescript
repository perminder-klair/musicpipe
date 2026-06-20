-- export-am-library.applescript
--
-- Run on the Mac that has the Apple Music library. Writes one
-- `artist<TAB>album` line per track in the library to ~/Desktop/am-library.tsv.
-- Dedup happens downstream in scripts/import-am-library.py.
--
-- Run with: osascript export-am-library.applescript
-- Or: open in Script Editor and hit play.
--
-- For large libraries (10k+ tracks) this may take a couple of minutes.

tell application "Music"
	set payload to ""
	repeat with t in (every track of library playlist 1)
		try
			set ar to (album artist of t)
			if ar is missing value or ar is "" then set ar to (artist of t)
			if ar is missing value then set ar to ""
			set al to (album of t)
			if al is missing value then set al to ""
			if al is not "" then
				set payload to payload & (ar as text) & tab & (al as text) & linefeed
			end if
		end try
	end repeat
end tell

set filePath to (POSIX path of (path to desktop folder)) & "am-library.tsv"
try
	set fh to open for access (POSIX file filePath) with write permission
	set eof fh to 0
	write payload to fh as «class utf8»
	close access fh
on error e number n
	try
		close access (POSIX file filePath)
	end try
	error e number n
end try

display notification "Wrote " & filePath with title "Apple Music library export"
