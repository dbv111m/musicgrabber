// =============================================================================
// Release Notes
// =============================================================================
// One entry per released version. Add new versions at the top.
// Shown once on first load after an update, and accessible via the
// "Release Notes" button in Settings. Keep it human-readable, not a
// changelog dump.

const RELEASE_NOTES = {
    "4.1.3": {
        title: "What's New in v4.1.3",
        sections: [
            {
                heading: "Housekeeping",
                items: [
                    "Amazon playlist importing used to read the page and also listen in on everything the Amazon app said to its servers, just in case. It turns out the page had it covered all along, so the eavesdropping has been shown the door. Same playlists, same running order, rather less lurking.",
                ]
            },
        ]
    },
    "4.1.2": {
        title: "What's New in v4.1.2",
        sections: [
            {
                heading: "YouTube Premium works properly again",
                items: [
                    "If you pay for YouTube Premium, MusicGrabber was not telling YouTube about it, so you were getting the ordinary quality stream instead of the good one. It now hands over your cookies properly and yt-dlp picks the Premium audio by itself. No new settings, nothing to switch on.",
                    "Everyone else benefits too: those cookies are also what keeps YouTube from stopping you with a 'confirm you are not a robot' check, and they were being left in the drawer on every single download since March.",
                    "If you set up cookies for your own account rather than globally, those were being ignored as well. They are not now.",
                    "Watched YouTube playlists now use your own cookies when checking for new tracks too. Previously they turned up with the shared credentials, which is a poor way to get into a private party.",
                    "Found, diagnosed and handed to us complete with the fix by dynAdZ, who noticed their Opus files were half the bitrate they were paying for. Thank you kindly.",
                ]
            },
            {
                heading: "Downloads outrank housekeeping",
                items: [
                    "If the database temporarily cannot be opened, failed attempts no longer leave later lookups waiting for imaginary connections. The waiting room has been cleared of ghosts.",
                    "Library tag indexing now stays firmly in the background. Downloads use the duplicate information already available and carry on immediately, even while a large NAS is still being indexed.",
                    "Track Upgrades now stops an active scan when switched off, and yields to the separate background duplicate index. Housekeeping has finally been reminded who pays the bills.",
                ]
            },
            {
                heading: "Your hard drives can have a lie-down",
                items: [
                    "Track Upgrades used to read every file in your library, from beginning to end, every single day, just to check nothing had changed. It now skips anything that has not moved or been edited since last time, and only opens files that genuinely look different.",
                    "If your library lives on a NAS or on spinning disks, this is the difference between hours of grinding every night and a few seconds of polite enquiry.",
                    "Every library scan used to ask your disks about the same file six to ten times over, having already been told. It now asks once and remembers the answer, which makes every scan in MusicGrabber roughly six times lighter on the drives.",
                    "The upgrade scan also stopped saving its notes to disk after every single track. A full pass over an unchanged library went from a hundred seconds to about thirty. Your NAS has not said thank you, but we know.",
                ]
            },
            {
                heading: "MusicGrabber has developed a memory",
                items: [
                    "There is now a proper index of your library: one line per track, kept in its own little database, quietly brought up to date once a day. Only files that are actually new or changed get opened. A track someone else renamed is recognised and keeps everything we already knew about it.",
                    "It lives on its own, away from your jobs and history. If it goes missing or gets confused, everything uses its existing fallback. A completed index still answers while it is being refreshed. No new settings, nothing to switch on.",
                    "Files moved or retagged by another organiser become visible at the next daily scan. Familiar filenames and known paths are still checked immediately.",
                    "Anything MusicGrabber does itself, though, it now writes down there and then: finishing a download, binning a track, fishing one back out, swapping in an upgrade, undoing an upgrade, or correcting tags from the Queue. No point asking the NAS tomorrow about a file we wrote ourselves this morning.",
                    "That last one also fixes a genuine bug. Correcting a track's tags used to leave its old name sitting in the index, so a completely unrelated download of that old name could be turned away as a duplicate of a track that no longer existed. It only became possible earlier in this very release, so nobody has actually been caught by it.",
                    "Stats storage totals and duplicate checks now use that memory. Finding a renamed track no longer means reading the entire collection again, and restarting MusicGrabber no longer gives duplicate detection amnesia.",
                    "Track Upgrades uses it too, and this is the big one. Most of your library was never downloaded by MusicGrabber, so it can never be upgraded by MusicGrabber, yet the scan still opened every last file every day to be reminded of that. It now just looks it up. A pass over the same music went from 34 seconds to 2.",
                    "The admin 'Clean rebuild' button reads it as well, so tidying up your Queue history no longer means trudging through the whole collection first.",
                    "The very first run has to read every file once, which on a big collection takes a while. It now says how it is getting on every thirty seconds, so you can tell the difference between working hard and having quietly died.",
                    "Fixed before anyone else could trip over it: on some network shares the index refused to build at all, quietly, forever, because of one obscure disagreement about how big a number is allowed to be. If your library lives on an SMB share and the storage figures never looked right, they will now.",
                ]
            },
            {
                heading: "With thanks to Michael Strong",
                items: [
                    "Michael independently built his own version of the library memory and shared it. Two ideas from it are in this release: the progress updates above, and a written-down way to make MusicGrabber rebuild its index from scratch if you ever suspect it has gone funny (it is safe, because everything in it comes from your music files).",
                    "Nice to be beaten to good manners by a stranger on the internet. Cheers, Michael.",
                ]
            },
        ]
    },
    "4.1.1": {
        title: "What's New in v4.1.1",
        sections: [
            {
                heading: "Jellyfin 12",
                items: [
                    "Jellyfin 12 changed the lock; MusicGrabber now uses its current API authentication, so connection tests and automatic library scans work again. Your existing API key is still fine.",
                ]
            },
            {
                heading: "Less waiting for the NAS",
                items: [
                    "Playlist choices and storage totals load in the background now, so the rest of the page can get on with its life. Loading and failed scans are labelled, and previous results stay visible during a refresh.",
                    "Duplicate checks now share one tag scan instead of all rummaging through the same library. If it takes more than five seconds, you get a clear retry message while indexing continues, rather than a download sitting there indefinitely. Incomplete checks never count as a missing track.",
                    "Fixed a premature playlist request around login, plus a late authentication error that could clear a newer session.",
                ]
            },
            {
                heading: "Tidier filenames",
                items: [
                    "Numbered track filenames now get a leading zero, so \"01 - Track\" sorts before \"10 - Track\" instead of after it. Big box sets pad a bit wider so nothing gets lost in the middle of a tracklist.",
                    "Fixed a rare one: some artist and track names use a fancy hyphen character that looks normal but isn't, and a few SFTP apps and older file shares would turn it into a \"?\" on disk. Those now save as a plain, boring, perfectly readable hyphen.",
                ]
            },
        ]
    },
    "4.1.0": {
        title: "What's New in v4.1.0",
        sections: [
            {
                heading: "The album browser is faster, and finally has pictures",
                items: [
                    "Browsing an artist's albums is now dramatically quicker for most artists, sometimes going from several seconds down to about one. Prolific, heavily-bootlegged artists (looking at you, Radiohead) will still take a few seconds.",
                    "Each album now shows a little cover thumbnail next to it, not just a wall of text. The album names still pop up straight away; the artwork fills in behind them a moment later.",
                    "Hover over a cover and it grows into a proper look at the artwork, then shrinks back down smoothly when you move away.",
                    "A few proper rarities have no official art anywhere online, so those just keep a blank square rather than us making something up.",
                    "The old \"Searching…\" and \"Loading albums…\" messages have also been retired in favour of a couple of dozen gentle digs at MusicBrainz for taking its time.",
                ]
            },
            {
                heading: "Queue history is easier to sift through",
                items: [
                    "The Queue now has a History picker for Failed first, Completed first, or Needs attention first.",
                    "Anything queued or downloading stays above the history whichever view you choose, so the work still in flight cannot disappear beneath yesterday's paperwork.",
                ]
            },
            {
                heading: "A failed download that succeeded on retry no longer argues with itself",
                items: [
                    "A retried download used to leave its old failed Queue card looking confused: still red and marked Failed, while its own details quietly admitted a later attempt had completed. The old card now tells its own story straight, and adds a \"Later succeeded on retry\" link straight to the one that made it.",
                    "The Copy button next to \"Why this result?\" now works the first time you click it, rather than needing \"Why this result?\" clicked first.",
                ]
            },
            {
                heading: "Restore now tidies the Queue too",
                items: [
                    "If you restore a rejected download from the Trash Bin, its Queue entry now changes from failed to completed. You picked that version after all, so the Queue no longer argues with you about it.",
                ]
            },
            {
                heading: "External organisers can rename things safely",
                items: [
                    "MusicGrabber now finds tracks by their embedded Artist and Title tags when the filename no longer matches. MusicBee, beets and friends may rearrange the shelves without triggering another download.",
                    "Each watched playlist can follow the global missing-file setting, re-download vanished files, or leave them alone.",
                    "Watched Artist cards now show moved or missing singles too, and append-playlist counts include tracks retained from earlier versions of the playlist.",
                ]
            },
            {
                heading: "Amazon Music album links work now",
                items: [
                    "Paste an Amazon Music album link into Bulk Import's Fetch box and it downloads as a proper album, with cover art and track numbers, the same as Spotify, Apple Music, YouTube Music, Beatport and Tidal already did.",
                    "Amazon was the one service left out when album links arrived in v4.0.3. Its album pages hand a browser a shell that never fills in, so there was no tracklist to read.",
                    "There did not need to be. MusicGrabber only needs the artist and album name; MusicBrainz supplies the tracklist once the record is matched, and the page title turns out to carry exactly that.",
                    "An album Amazon will not serve, whether delisted, region-locked or just a wrong link, now fails honestly instead of guessing at the wrong record.",
                ]
            },
            {
                heading: "YouTube Music album links that stump us now fall back to a playlist",
                items: [
                    "If MusicGrabber can't confidently match a YouTube Music album to MusicBrainz, it now imports it as an ordinary playlist instead of just giving up.",
                    "That is how these links always used to work anyway, before album matching existed, so an unmatched one now behaves at least as well as it did before.",
                ]
            },
            {
                heading: "Manually searched tracks link to their watched playlist right away",
                items: [
                    "Search for a track by hand and route it into a watched playlist, and Retry or Replace on it now work immediately, rather than only after the download finished.",
                ]
            },
            {
                heading: "Cleared Queue rows no longer leave stale playlist links",
                items: [
                    "Clearing Queue history now removes its job links from watched-playlist tracks too, including dangling links left by older releases.",
                    "Replace now identifies the playlist track directly, so it still trashes the right file and rebuilds the M3U after the old Queue row has gone.",
                    "The track stays marked downloaded when history alone is cleared. Only Replace or a real missing-file check changes that state.",
                ]
            },
        ]
    },
    "4.0.4": {
        title: "What's New in v4.0.4",
        sections: [
            {
                heading: "\"Create M3U\" off now actually means off",
                items: [
                    "A watched playlist with Create M3U disabled was still getting an .m3u file made for it, and added to after every download. It now gets neither: no new file, and an existing one is left exactly as you left it.",
                    "Two different bits of code write that file. The full rebuild checked the setting; the per-download append did not, so the setting held until something downloaded.",
                    "That append also added tracks in the order they finished downloading rather than the playlist's own order, so the file drifted out of order as well. Turning Create M3U back on and refreshing rebuilds it properly, as before.",
                    "Downloading tracks into the playlist folder is unaffected; that never had anything to do with the M3U. Reported in issue #82.",
                ]
            },
            {
                heading: "Replace works again on older playlist tracks",
                items: [
                    "Replace on a watched playlist track could fail with 'Failed to trash file' when the file was plainly there and perfectly writable.",
                    "The cause was a missing database row, not a missing file: Replace works through the original download job, and clearing completed jobs removes those rows while the playlist track carries on pointing at them.",
                    "The playlist track knows where its own file lives, so that is what gets used when the job has gone. The file is trashed, the track goes back to missing, and you carry on to the search step as usual.",
                    "Error messages from this button now say what actually went wrong rather than always blaming the trash. Reported in issue #83.",
                ]
            },
            {
                heading: "Amazon playlists keep their running order",
                items: [
                    "Watched Amazon Music playlists were coming back sorted alphabetically by artist instead of in playlist order. They now arrive in the order Amazon shows them.",
                    "The order comes from the position Amazon's own list gives each row, so it holds up even on long playlists where the page recycles rows as you scroll.",
                    "Existing watched Amazon playlists sort themselves out on their next refresh. Thanks to the reporter of issue #81, who had already found the exact line at fault.",
                    "Explicit tracks were also picking up a stray 'E' on the end of the title, from the badge Amazon puts next to them, and were being searched for under that name. Not any more.",
                ]
            },
            {
                heading: "Albums finally know what year they are",
                items: [
                    "Album downloads now tag every track with the release year, taken from the album you picked rather than each track guessing for itself.",
                    "Previously the tracks each asked MusicBrainz separately, so one album could end up with several different years and a few blanks. They now all agree.",
                    "It is the year of that particular pressing, so a remaster says so instead of borrowing the original's date. The year is written into the .albuminfo sidecar as well.",
                    "Albums already on disk are untouched; this applies to new downloads.",
                ]
            },
            {
                heading: "Stop re-downloading files another tool has moved",
                items: [
                    "Settings, under General: 'Re-download Missing Files'. On by default, so nothing changes unless you turn it off.",
                    "Watched playlists and artists have always re-fetched a track whose file they can no longer find, which is right if you deleted a bad rip and wrong if MusicBee, beets or similar moved it somewhere we cannot see. In that case we download it again, your organiser tidies the copy away, and the pair of them keep each other busy indefinitely.",
                    "Renaming is enough to trigger it, by the way. The file never has to leave your music folder for us to lose track of it.",
                    "Turn the setting off and a track downloaded once stays downloaded. The Retry button in a playlist's Missing panel still fetches one back when you actually want it.",
                    "Playlist cards will tell you how many tracks are in this state, and warn that they will not appear in the M3U. The count clears itself if the files turn up again.",
                ]
            },
            {
                heading: "Preview button on desktop",
                items: [
                    "Settings, under General: 'Preview Button on Desktop' puts the Preview button that phones and tablets already get onto desktop results too.",
                    "With it on, a preview only starts when you press the button. Resting the mouse on a result no longer sets one off, and the preview carries on playing when the mouse wanders somewhere else.",
                    "Off by default, and remembered in your browser rather than on the server, so each machine you use can decide for itself.",
                    "The button now says what it is doing: 'Loading...' while the URL is fetched, 'Stop' while it plays, back to 'Preview' when it is done.",
                    "Starting a different preview cancels the old request, and pressing a loading button again cancels it instead of fetching the same preview twice.",
                ]
            },
            {
                heading: "MP3Phoenix grows up",
                items: [
                    "It is no longer labelled experimental, because it works. Still off by default, since it briefly launches Chrome for Cloudflare clearance and that is not something an upgrade should spring on you.",
                    "MP3Phoenix results can be previewed now, the last source that could not be. Only the opening 25 seconds or so is fetched, and it comes through MusicGrabber rather than straight from the provider, because Cloudflare will not serve a browser directly.",
                    "A stale or invalid preview can fail without tripping MP3Phoenix's shared ten-minute search and download cooldown.",
                ]
            },
            {
                heading: "YouTube previews work again",
                items: [
                    "YouTube stopped letting browsers stream the media URLs that the yt-dlp version in the image produced, so previews went quiet while downloads carried on perfectly happily. A current yt-dlp resolves them differently and previews play again.",
                    "The container now updates yt-dlp when it starts, so this sort of thing fixes itself rather than waiting for a new image. It is bounded and never fatal, and YTDLP_AUTO_UPDATE=false turns it off if you would rather pin the shipped version.",
                    "Preview requests also wait up to 30 seconds instead of 15, since YouTube's player challenge can take a while to solve.",
                ]
            },
        ]
    },
    "4.0.3": {
        title: "What's New in v4.0.3",
        sections: [
            {
                heading: "Plex library scan",
                items: [
                    "Add a Plex URL and token in Settings and MusicGrabber will nudge Plex to rescan its music library after every download, same as it already does for Navidrome and Jellyfin.",
                ]
            },
            {
                heading: "Track Upgrades leave a trace",
                items: [
                    "A completed upgrade used to vanish without a trace: the Queue entry for the download was deleted the instant the swap finished, so there was nothing to look back on. Completed upgrades now stick around in the Queue like any other finished download, marked with an UPGRADE badge, pointing at the file that's actually sitting in your library.",
                ]
            },
            {
                heading: "Track Upgrades: quarantine, and honest results",
                items: [
                    "New Quarantine panel on Watched Upgrades. Every file an upgrade swapped out is listed there, with Restore to put it back (whatever replaced it goes to quarantine in turn, so you can undo the undo) and Delete when you're sure. No more rummaging around in a hidden folder by hand.",
                    "Upgrading a track no longer loses its Artist and Album tags. They were being overwritten with the name of a temporary working folder, which is exactly as useful as it sounds.",
                    "An upgrade that lands better but still below your target quality now stays on the list and says so, instead of disappearing and mysteriously reappearing later.",
                    "\"Upgrade All\" now shows progress as it works and refreshes the list when it's done, rather than asking you to keep hitting refresh yourself.",
                ]
            },
            {
                heading: "Tidal playlists are no longer cut off at 50 tracks",
                items: [
                    "Tidal quietly capped the page we read playlists from at the first 50 tracks, so a 204-track playlist arrived as 50 and looked perfectly fine. Playlists now come from Tidal's own API, in full, however long they are. Monochrome playlists get the same fix, since they're Tidal playlists underneath.",
                    "Nothing to re-add: the missing tracks turn up on the next refresh and download themselves. If you use mirror sync, the ones that got dropped from your M3U come back too.",
                ]
            },
            {
                heading: "More album links understood",
                items: [
                    "Paste a YouTube Music, Beatport, Tidal or Monochrome album link into Bulk Import and it now gets treated as a proper album: matched against MusicBrainz and filed under Albums with cover art and track numbers, the same as Spotify and Apple Music links already were. Previously these were either flattened into a plain track list or refused outright.",
                ]
            },
            {
                heading: "Smaller things",
                items: [
                    "You can empty your own Trash without being an admin. It's your bin.",
                    "Finishing a track upgrade now tells Jellyfin and Plex to rescan too, not just Navidrome.",
                ]
            },
        ]
    },
    "4.0.2": {
        title: "What's New in v4.0.2",
        sections: [
            {
                heading: "Track Upgrades fixes",
                items: [
                    "Soulseek upgrades no longer fail right after a successful download. The upgrade worker was mistaking the file it was about to replace for a duplicate and throwing the replacement away; it now knows better.",
                    "Opus upgrades were being wrongly rejected as \"not actually better\" because Opus files don't carry a readable bitrate, so quality comparisons couldn't tell a fresh lossless-sourced copy from a mystery file. New downloads now stamp the real encode rate into the file so the comparison works properly.",
                    "Watched Upgrades no longer keeps listing files it already searched and found nothing for. Those drop off the list instead of sitting there as a permanent dead end.",
                ]
            },
        ]
    },
    "4.0.1": {
        title: "What's New in v4.0.1 - Fix of the Phoenix",
        sections: [
            {
                heading: "Play buttons play things again",
                items: [
                    "The play buttons in the Queue and the Trash used to answer \"Could not play file\" no matter how healthy the file was. The browser's audio player cannot send a login header, so its request arrived unauthenticated, was refused, and the player blamed the file. Playback now carries its own short-lived key and works properly, seeking included.",
                    "This only ever affected installs with two or more accounts. Single-user installs skip logins entirely, so it quietly worked there the whole time.",
                ]
            },
            {
                heading: "Security and multi-user fixes",
                items: [
                    "Download and preview requests now verify that the URL genuinely belongs to the source it claims to come from. Previously the server would fetch whatever URL it was handed, which on a shared or internet-facing install was rather more trusting than it should have been.",
                    "Custom playlist folders, and the per-user Singles, Playlists and Albums folder settings, can no longer point outside your music directory. Absolute paths and \"..\" are refused with a clear message rather than quietly writing somewhere unexpected.",
                    "The trash bin is now per-account. It used to be shared, meaning anyone with an account could browse, play, restore or permanently delete anyone else's deleted music. Existing trash is moved into the first admin's bin on upgrade, so nothing is lost; single-user installs are unaffected.",
                    "Watched Artists now checks the correct user's library when deciding whether a track is already on disk, and Soulseek will no longer deliver, or in move mode delete, a same-named file of the wrong size.",
                    "Force Accept checks who owns the job before changing anything.",
                ]
            },
            {
                heading: "Albums, queueing and file handling",
                items: [
                    "Various Artists compilations now search for the artist who actually performed each track, instead of looking for songs by \"Various Artists\". The album artist is still recorded as the album artist.",
                    "Double-clicking an album download or a retry no longer queues the same thing twice; the second one is told an import is already in progress.",
                    "Serving, retagging, moving or deleting a track now confirms the file is still a real file inside your library first, rather than trusting a stored path that may no longer point anywhere sensible.",
                    "Rebuilding the library path index no longer risks aborting partway through on very large libraries.",
                ]
            },
            {
                heading: "Monochrome can now be tested from Settings",
                items: [
                    "A new \"Test Downloads\" button in the Monochrome settings section downloads a real track end to end, checks the audio is genuine, then deletes it. Every step is reported separately, so you can see exactly which route failed instead of guessing.",
                    "Common causes get a plain-English suggestion: not enough shared memory for Chromium, a missing browser, a Turnstile timeout, an exhausted token pool, or a container that cannot reach the internet.",
                    "It tests your saved settings, so save any changes first. Around ten seconds when everything is healthy, a minute or two if the browser session has to start from cold.",
                ]
            },
            {
                heading: "Monochrome previews are back",
                items: [
                    "The retired public Qobuz proxy layer has been removed. Monochrome now goes straight to its browser-authenticated playback route, then direct qbdlx and Tidal fallbacks, without probing or retrying endpoints that can no longer serve a track. The obsolete proxy URL setting disappears automatically.",
                    "Hovering a Monochrome result plays a preview again, roughly two seconds from hover to sound. Monochrome only serves encrypted FLAC-in-MP4, which no browser can play, so MusicGrabber decrypts the first thirty seconds to MP3 and serves that itself. Snippets are cached briefly, then deleted.",
                    "The browser session is warmed once in the background at startup, so a hover never waits for Chrome to start and never queues behind a download in progress. That warm-up only runs when Monochrome is an enabled source with the browser fallback switched on; turn either off and no browser is started at all.",
                    "Downloads now try Monochrome's own infrastructure before the shared Qobuz token pool (qbdlx) rather than after, since that pool has been running thin. qbdlx remains the fallback for when Monochrome's own route comes up empty.",
                    "The search box's clear cross is no longer sat on by the Search button when it widens to say \"Searching…\".",
                    "Monochrome playlist share links (monochrome.tf/playlist/...) fetch their tracks again, now via the same direct Tidal route plain tidal.com playlist links already use, since the hifi-api gateway they relied on has stopped working.",
                ]
            },
            {
                heading: "Split long DJ mixes into proper tracks",
                items: [
                    "Paste a single YouTube video into Bulk Import's Fetch box and, if it's over 12 minutes, MusicGrabber checks whether it looks like a mix or compilation. It never guesses where one track ends and the next begins: it uses YouTube's own chapters when they exist, then a hand-typed cue sheet in the video's description, then one in the comments, and otherwise gives you a blank table (or a box to paste your own cue sheet into).",
                    "Either way you always get an editable preview, start time, end time, title and artist per track, before anything downloads. Confirming it cuts each track with ffmpeg, tags it from what's in the table, and files the lot under Albums with cover art from the video thumbnail.",
                    "Pasting a playlist or album link into the ordinary Search box now sends it to Bulk Import instead of quietly going nowhere, since that's the box that actually knows what to do with a URL.",
                ]
            },
        ]
    },
    "4.0.0": {
        title: "What's New in v4.0.0: Rise of the Phoenix",
        sections: [
            {
                heading: "Rise of the Phoenix",
                items: [
                    "MP3Phoenix is back as an experimental search and download source. It remains disabled by default; turn it on under Settings, Search Sources if another decent MP3 fallback is worth a little browser theatre.",
                    "SeleniumBase briefly clears Cloudflare, then curl_cffi reuses the browser cookies and user-agent with Chrome's network fingerprint for efficient searches and streamed MP3 delivery. The session lasts at most an hour; a challenge refreshes it immediately and retries once.",
                    "Three consecutive browser failures park the source for ten minutes and automatic work falls through normally. Downloads still face MusicGrabber's integrity, duration, metadata, conversion, duplicate, and fallback checks before reaching the library.",
                    "There is no hover preview for MP3Phoenix. Spending the guarded provider session because a mouse rested on a card would be an impressively expensive tooltip.",
                ]
            },
            {
                heading: "Maintenance, mobile, and one honest YouTube fallback",
                items: [
                    "Settings now previews database housekeeping before you run it: orphan imports, selected Queue history, missing-file reconciliation, and a clean path-index rebuild. None of these actions deletes or moves library audio.",
                    "YouTube playlist tracks can optionally fall back to their exact original video after every confident alternative fails. It is off by default, and the Queue labels it when used.",
                    "On phones the header drops version and username text first, then wraps only on the narrowest screens, so Sign out no longer wanders beyond the edge.",
                ]
            },
            {
                heading: "A new Artists tab, and the Albums tab has retired",
                items: [
                    "The tab bar is now Tracks, Bulk Import, Artists, Watched, Queue, Stats, Settings. Albums was a way of finding things rather than a place, so its artist browsing, duplicate checks and album downloading have moved into Bulk Import and the new Artists tab rather than going anywhere.",
                    "Watched Artists has moved into Artists, with everything it did before intact: the same artist picker, the same \"watch future singles from this date\", the same intervals and controls.",
                    "Watched is now just Watched Playlists and Watched Upgrades, plus the Audio Provenance Audit. Searching for one track and grabbing it is exactly as quick as it was; that bit was never the problem.",
                ]
            },
            {
                heading: "Find the artist, then decide what you want",
                items: [
                    "Picking an artist now offers you three things rather than assuming one: Follow for Singles, Follow for Albums, or Download an Album with no strings attached.",
                    "Follow for Albums lays out the whole discography with tick boxes, so you can queue five records in one go instead of picking one, waiting, going back and picking the next. Albums already in your library are marked as such, so you cannot accidentally download your own collection back to yourself.",
                    "Singles and albums are independent, so you can follow an artist for one, the other, or both. An albums-only follow leaves their singles well alone, which for a prolific artist is the difference between a few albums and four hundred B-sides.",
                    "Every artist you already follow carries on watching singles exactly as before. Nothing was quietly unfollowed.",
                ]
            },
            {
                heading: "Artists can collect new albums on their own",
                items: [
                    "Following for albums includes an opt-in \"automatically download future albums\"; leave it off and you simply get whatever you ticked.",
                    "Switching it on does not download the back catalogue. Everything already released is quietly marked as seen, and only albums that appear afterwards get picked up. A toggle that might start a fortnight of downloading is a toggle nobody would dare tick.",
                    "Live albums, compilations, soundtracks, DJ mixes, demos and remix collections are filtered out, using the same rules the singles watcher already applies. Remasters, reissues and deluxe editions are left in on purpose, since they are as often the thing you wanted as they are noise.",
                    "Albums you tick by hand are recorded as dealt with, so the automatic side never queues them a second time.",
                ]
            },
            {
                heading: "Bulk Import now takes almost anything",
                items: [
                    "One box that works out what you gave it: playlist URLs, pasted \"Artist - Title\" lists and MusicBrainz links all behave as before, and Spotify and Apple Music album URLs now work too.",
                    "Anything recognised as an album goes through the album pipeline, so it arrives in Albums/Artist/Album/ with artwork, track numbers and an .albuminfo sidecar, rather than being flattened into a heap of unrelated singles.",
                    "You can also just type a name. Album search no longer needs an artist first, so \"Trainspotting\" or \"Now That's What I Call Music 42\" work fine, and results are labelled Soundtrack, Compilation or Live so you can tell what you are looking at.",
                    "Matching a streaming album to MusicBrainz is fuzzy work, and getting it wrong costs you a whole wrong album rather than one wrong track. Uncertain matches stop and ask which one you meant, and even a confident match shows you what is about to be queued first.",
                    "YouTube, Amazon, Beatport and Monochrome album URLs are not supported yet, and say so plainly rather than half-working.",
                ]
            },
            {
                heading: "Monochrome lossless downloads work again",
                items: [
                    "Monochrome put its playback behind a Cloudflare Turnstile check, which quietly locked MusicGrabber out and left the Qobuz routes unable to help.",
                    "There is now an optional fallback that opens a brief Chrome session, completes the same check a visitor gets, and asks for one authorised audio resource. Only that small request happens in the browser; the audio downloads normally afterwards, and protected lossless files are decrypted and remuxed to a clean FLAC before anything else sees them.",
                    "It is a last resort, not a habit: the browser is never launched for previews, only one session runs at a time, and it takes a few tens of seconds per track. You can turn it off in Settings under Monochrome if you would rather it never started Chrome at all.",
                    "The container image is larger as a result, since it now ships a browser for this on top of the one used for Spotify.",
                ]
            },
            {
                heading: "Soulseek tracks are credited to the artist, not the stranger",
                items: [
                    "A Paramore track was arriving tagged as being by \"2jqll9htuy62asp1wu\", and filed in the library under that name too.",
                    "The artist was sitting in the file's own path the whole time; the search results simply dropped it before reaching the browser, which then fell back to the name of whoever was sharing the file. Results now carry both, so you can see who you are downloading from without them being credited as the artist.",
                    "Paths are only guesses too: real shares put folders such as Music (FLAC), complete, Musique, and single-letter buckets before the artist. Downloads now wait in private staging while MusicGrabber reads the file's own ARTIST tag, and only then are checked for duplicates, looked up, tagged, and filed. Untagged files retain the path fallback.",
                    "Retrying an older, wrongly-tagged job will not bring it back either; an artist that is just a Soulseek username is now refused outright.",
                ]
            },
            {
                heading: "You can choose when singles watching starts",
                items: [
                    "Following an artist for albums only still quietly stored a start date: the day you followed them. Switch Singles on later and that forgotten date decided how much of their back catalogue arrived, with nothing in the interface offering to change it.",
                    "Flipping the Singles toggle now asks which date to watch from, defaulting to today. Nothing is saved until you confirm, so cancelling really does leave everything exactly as it was.",
                    "Once singles are on, the date sits on the card as an editable field. Wind it back to pick up last year's releases, or push it forward for a quieter life.",
                ]
            },
            {
                heading: "YouTube downloads that used to fail for no visible reason",
                items: [
                    "The container now ships a JavaScript runtime (deno), which yt-dlp needs to talk to YouTube properly. Without it YouTube hands back a shortened list of formats, and a perfectly available track fails with \"format manifest unavailable for this request\".",
                    "Michael Jackson's Thriller failed that way every time, which is how it got spotted. It downloads now.",
                    "YouTube throttling is a separate matter and still very much itself; searches occasionally come back with nothing. No runtime fixes that.",
                ]
            },
            {
                heading: "EPs count as records now",
                items: [
                    "Clicking \"Haunted House\" on a Knife Party search result used to insist MusicBrainz had never heard of it. MusicBrainz had; the album list was only ever asking for things typed \"Album\", and Haunted House is an EP, as is nearly everything Knife Party has released.",
                    "Their whole discography therefore came back as one record, and you were invited to pick the right album from a list of one. Artists who release EPs for a living came off worst.",
                    "Artist album lists now include EPs, tagged as such so you can tell them apart. Knife Party goes from one entry to six. Compilations, live albums, soundtracks, DJ mixes and remix records are still kept out, EPs included.",
                    "Singles are still left out on purpose: a prolific artist's list would otherwise become a hundred one-track entries standing between you and the actual records.",
                    "If you follow an artist with \"automatically add new albums\" switched on, their EPs now count as new music. Fair warning: the first check after upgrading also spots the EPs they had already released, and will queue them.",
                ]
            },
            {
                heading: "Soulseek results now tell you what they are",
                items: [
                    "Every Soulseek result now shows the artist, the album and its year, the file size, the bitrate, the album cover, and a match percentage telling you how closely the shared file matches what you searched for.",
                    "Almost all of it was already in the search response and being thrown away. Cover art is the one genuine addition, looked up once per album rather than once per result, so twenty results from the same record cost one lookup.",
                    "The bitrate on a lossless file is worked out from its size and duration rather than taken on trust. A \"24bit/96kHz\" FLAC quietly running at 900 kbps is an upscale, and now you can see that before you download it.",
                    "Only Soulseek shows a match percentage, because only Soulseek has a real number to put there.",
                    "Rip labels such as [FLAC], [1733 kbps], and [16-44] are cleaned off titles, complete release dates no longer leave half a date in the album name, and bracketed shelf labels are reduced to the artist you actually searched for.",
                    "The card now shows the peer's queue depth too. A busy queue lowers that result slightly without hiding it, so an idle peer wins a tie but the only good copy remains available.",
                    "Album artwork now falls back through Deezer's actual album search when iTunes has nothing, rather than asking its track search to guess what an album title meant.",
                ]
            },
            {
                heading: "Artists in Soulseek results are read properly, not guessed",
                items: [
                    "A search for \"paramore - ignorance\" used to credit four of its top five results to \"Musique\", \"FLAC Archive\", \"02 - Ignorance.flac\" and \"P\": a language, a format, a filename and an alphabetical bucket.",
                    "Everyone files their music differently, so MusicGrabber now uses the fact that you already told it who you wanted, and goes looking for them in the path rather than guessing from position. All five now say Paramore.",
                    "The same pass reads the album, year and track number, coping with \"Artist - Album\", \"[2009] Album\", \"CD1\" subfolders and underscores-instead-of-spaces. Albums that are just a year, like Taylor Swift's 1989, keep their names.",
                    "Related searches no longer offer to find you more music by \"2jqll9htuy62asp1wu\".",
                ]
            },
            {
                heading: "Soulseek downloads can move instead of copy",
                items: [
                    "New opt-in toggle under Settings, Soulseek: \"Move completed downloads\". Off by default, so nothing changes unless you want it to.",
                    "Leaving it off keeps today's behaviour: the file is copied and slskd's own copy stays put, so you carry on sharing it back to the network. Turning it on saves you keeping two copies of everything.",
                    "Moving is instant when both live on the same mount. Across mounts it copies, checks the result by size and SHA-256, and only then deletes the original. If that check fails the original stays exactly where it was, so nothing is lost.",
                    "Folders left empty are tidied up afterwards, stopping the moment anything else is in them.",
                ]
            },
            {
                heading: "Track numbers stop coming from radio promo compilations",
                items: [
                    "A correctly tagged \"track 2 of 14\" was arriving in your library as track 15 of 16. Files that already carry their own track number now keep it; MusicGrabber only fills in what is genuinely missing.",
                    "The cause underneath: when a recording is only known to MusicBrainz from a compilation, that compilation was being used to decide the album and track position. Compilations, live albums, soundtracks, DJ mixes and Various Artists releases are now refused for that purpose. The artist and title still come through; the album is simply left blank rather than confidently wrong.",
                ]
            },
            {
                heading: "Convert to now names the format it actually uses",
                items: [
                    "The old switch was called convert_to_flac even when the selected target was Opus, MP3, or ALAC. It is now convert_audio from the browser all the way down to the database columns, which are renamed on first boot with your settings intact. The old name is still accepted from API clients, still returned in responses, and DEFAULT_CONVERT_TO_FLAC still works in your compose file.",
                    "Settings now warns when conversion would turn a lossless source into Opus, MP3, or lossy AAC-in-M4A. Keep source remains the shipped default, and is the choice that leaves a genuine FLAC exactly as it arrived.",
                ]
            },
            {
                heading: "Soulseek results show up again",
                items: [
                    "Reported by Syrius-consulting: slskd ran the search, found hundreds of files, reported it complete, and MusicGrabber showed nothing at all.",
                    "A rename in v3.1.0 was only half applied, so the moment a file was good enough to be worth having, the code that scores it fell over and took the whole batch with it. Searches that matched nothing well never hit that line, which is how it went unnoticed for a release.",
                    "Fixed, and the quality bonuses now actually count, so lossless and hi-res shares outrank a 320 of the same track. Anyone on v3.0.3 or earlier was never affected.",
                ]
            },
            {
                heading: "Searches wait their turn instead of giving up",
                items: [
                    "Each source allows one search at a time. That queue existed but was never used: anything arriving while a source was busy got an instant refusal, which to a bulk import looks exactly like \"nobody has this track\", and the track was then written off for good.",
                    "Searches now queue politely. Six simultaneous searches for the same source used to leave five with nothing; they now all come back with results, one after the other.",
                    "\"Busy\" is also now told apart from \"nothing found\", so a track is never marked missing over a search that never actually happened. Searching a single source that is genuinely busy says so and asks you to try again shortly, rather than reporting a server error.",
                ]
            },
            {
                heading: "Stuck imports get picked back up, or properly buried",
                items: [
                    "An import whose worker died left a row claiming to be processing forever. One test instance had 36 of them, the oldest dating from January, and between them they were quietly making searches fail.",
                    "Imports now have a heartbeat. One that goes quiet for an hour is resumed where it left off, and restarts are handled at boot rather than after an hour of nothing happening.",
                    "It knows when to stop, too: three revivals maximum, nothing older than a week is resurrected, and nothing is resumed for a watched playlist you have already deleted.",
                ]
            },
            {
                heading: "Albums now arrive as albums",
                items: [
                    "RAYE has two albums that both end on a track called \"Fin.\". Downloading the 2023 one, MusicGrabber searched for the track by name, got two results it could not tell apart, picked the wrong one and filed it as track 15 with every appearance of a job well done.",
                    "Album downloads now match the release as a whole first and take each track from that one album, so a track can only come from the record you asked for. Anything it cannot place, and any album not carried by the lossless sources, falls back to the old per-track search exactly as before.",
                    "A 17-track album that previously came back with 4 tracks now comes back with 17.",
                ]
            },
            {
                heading: "A source having a moment no longer loses you the track",
                items: [
                    "Queueing two albums at once made them race each other, and the loser was told \"busy\" by every source at the same instant. That looked exactly like \"nobody has this track\", and a track marked failed never got another look.",
                    "Empty searches now get three attempts with a widening pause between them, so a momentary collision costs a few seconds rather than thirteen tracks. Something nobody genuinely has still fails, it just takes three goes to say so.",
                ]
            },
            {
                heading: "Download cards stop crediting the wrong source",
                items: [
                    "A track could show a YouTube badge and link while the file on disk was a Monochrome FLAC. The YouTube copy had been rejected for being the wrong length, quietly re-fetched from Monochrome, and the card never caught up.",
                    "The \"Duration\" line on those cards was how long the job took, not how long the track is, which sitting under \"Stored\" and \"Original\" was needlessly alarming. It now says \"Took\".",
                ]
            },
            {
                heading: "Artist search no longer loses artists who use capitals",
                items: [
                    "Searching \"Raye\" gave you one result, and it was the wrong one: an anonymous feature credit on a Dead Prez record. RAYE the English singer was there all along, scoring higher than any of them, but spelling herself in capitals pushed her to fourth and the picker only ever showed the first.",
                    "Relevance now leads, with capitalisation used only to break ties. Where several artists genuinely share a name, as four do with \"Raye\" and three with \"Adele\", you now see them all with the text that tells them apart.",
                    "Artists with an unambiguous name, Radiohead and the like, still resolve straight to one result and one click.",
                ]
            },
            {
                heading: "Watched playlist counts finally match the playlist",
                items: [
                    "A 150-track playlist could report 244 tracks with 158 downloaded, because every track ever seen was counted, not the ones actually in the playlist. Charts that change weekly were the worst affected: a Top 100 reported 402.",
                    "Cards now show the playlist as it stands, plus anything left over listed separately as \"kept from earlier\" (append mode) or \"no longer upstream\" (mirror mode). No audio file is deleted either way; append mode still keeps its history and its M3U entries.",
                    "In mirror mode, the Missing panel no longer lists tracks that have left the playlist, and the track list's summary now agrees with its own section headings.",
                    "Turning the M3U toggle off stops MusicGrabber updating that .m3u, but any file already written stays put, which is now stated on the toggle rather than left as a surprise.",
                    "Thanks to Tristan for the detailed report, examples included.",
                ]
            },
            {
                heading: "\"database is locked\" errors have been evicted",
                items: [
                    "Bulk imports could die with sqlite3.OperationalError: database is locked, and changing the preferred source on some watched playlists could silently refuse. Same cause, and it was not the database being busy in the way it appeared.",
                    "A query whose rows were not all read left a snapshot open on that pooled connection. As soon as anything else saved, the next write on that connection failed immediately, and because the pool hands back the most recently used connection first, the affected one went straight out to the next caller. Connections are now tidied up when they are returned to the pool, and a write that still meets a lock gets retried rather than giving up on the spot.",
                    "A bulk import also no longer abandons the tracks it has not reached yet when one track goes wrong; recording the failure can no longer become the failure.",
                    "Thanks to Tristan for the report and for digging into the logs.",
                ]
            },
            {
                heading: "\"Guess Again\" stops handing out rehearsal tapes",
                items: [
                    "The tag editor's Guess Again button searched MusicBrainz by text alone and took whichever recording scored highest for raw relevance, with none of the safeguards an ordinary download already has.",
                    "Re-guessing The Beatles' \"Let It Be\" could hand back a rehearsal take filed on the Artifacts II bootleg box set, complete with the wrong year and the wrong track number.",
                    "It now shares the same recording picker an ordinary download uses, and tries matching the actual audio file with AcoustID first when it is available, rather than trusting a text search alone.",
                ]
            },
        ]
    },
    "3.1.0": {
        title: "What's New in v3.1.0",
        sections: [
            {
                heading: "Email notifications can now introduce themselves",
                items: [
                    "SMTP settings now have a Send Test Email button. It uses the values currently in the form, so you can check a change before saving it, while still using an already-saved password when that field is hidden.",
                    "A failed test now says whether DNS, the connection, STARTTLS, authentication, sender or recipient was rejected instead of silently doing nothing. The toggle is also labelled Use STARTTLS now: it is intended for port 587, not implicit TLS on port 465.",
                ]
            },
            {
                heading: "Downloading the same track twice at once no longer breaks both",
                items: [
                    "Every download used to write its working files straight into the destination folder, all named after the track. Two jobs for the same song at the same time would therefore delete each other's half-finished files, and whichever one next reached for a file the other had tidied away failed with a baffling \"No such file or directory\". This was reachable whenever the same track sat in two watched playlists refreshing together, or turned up twice in one bulk import.",
                    "Each download now works in its own staging area and only the finished audio moves into your library, so two jobs simply cannot reach each other's files. Anything left behind by a hard restart is cleared on startup.",
                    "This had been filed as a flaky thumbnail server, because cover art is fetched first and so was usually the first casualty. It was never the CDN's fault.",
                ]
            },
            {
                heading: "A missing thumbnail no longer costs you the track",
                items: [
                    "Separately, yt-dlp converts cover art before it downloads any audio, so if the thumbnail was genuinely missing, the whole download was abandoned before a single byte of music arrived, leaving nothing on disk to rescue.",
                    "A download that trips over cover art now gets one more attempt with thumbnail embedding turned off. You lose nothing by it: MusicGrabber fetches proper album art from Cover Art Archive, iTunes or Deezer straight afterwards and replaces the video thumbnail anyway.",
                ]
            },
            {
                heading: "Starting up now takes about a second",
                items: [
                    "The container used to sit there for 30 seconds or more before the web page would load at all. The cause was gloriously self-inflicted: the database connection pool only fills up as connections are handed back, so at boot it is empty, and the code read empty as everyone is busy and waited out a full 15-second timeout before opening the connection it could have had straight away.",
                    "The pool now grows on demand and only waits when every connection is genuinely in use, which is the situation it was actually written for. Startup housekeeping has also moved into the background thread that already runs it on a schedule, so checking your library for deleted files no longer holds the door shut.",
                    "Measured on a local container: 15.9 seconds down to 0.8, and under a second on a restart.",
                    "A small loading panel now appears the moment the page arrives, so the brief gap while the app finishes waking up no longer looks like something has broken. It also picks up your saved theme before the first paint, sparing light-theme users a flash of the void.",
                ]
            },
            {
                heading: "ReplayGain, without touching the audio",
                items: [
                    "New downloads can now be measured and tagged with ReplayGain 2.0 track gain and peak, referenced to the standard -18 LUFS. Settings → Write ReplayGain Tags turns it on; it is off by default.",
                    "Nothing is re-encoded. This is the polite cousin of Normalise Loudness: it writes down how loud the track is and leaves your player to act on it, which means it is safe on lossless files and can be undone by deleting four tags.",
                    "Album gain and album peak are added once every track on a record has arrived, worked out across the whole album so the quiet track five stays quieter than the singles instead of being levelled flat.",
                    "Files that already carry ReplayGain tags are left exactly as they are, unless you turn on Replace Existing ReplayGain Tags. FLAC, MP3, M4A, Ogg and Opus are supported.",
                ]
            },
            {
                heading: "Watched schedules have learned to read a clock",
                items: [
                    "A playlist or artist set to every 30 minutes now really is checked every 30 minutes. Previously its own interval only decided whether it was due after the global scheduler woke, and that scheduler slept for 24 hours by default.",
                    "Adding, resuming, deleting, or changing a watch wakes the scheduler immediately to recalculate its next deadline. Restarting the container is no longer an accidental scheduling control.",
                    "Last checked now says just now, Xm ago, Xh ago, or Xd ago, with the exact local timestamp on hover. The old date-only display made every same-day refresh look as though it never happened.",
                ]
            },
            {
                heading: "Paste a MusicBrainz album link, get an album",
                items: [
                    "Bulk Import now accepts musicbrainz.org release and release-group URLs, and sends them down the proper album pipeline: Albums/Artist/Album/, cover art, an .albuminfo sidecar and dependable track numbers.",
                    "Because the MBID is already in the URL there is nothing to scrape and nothing to guess. Release-group links (the ones MusicBrainz search hands you) resolve to the earliest official pressing.",
                ]
            },
            {
                heading: "A minimum-quality filter, with its cards on the table",
                items: [
                    "The Tracks tab can now hide anything below 192, 256, 320 kbps or lossless.",
                    "It is upfront about what it cannot know: only Soulseek, Monochrome and FreeMp3Cloud state a quality before you download. YouTube and SoundCloud say nothing, so they count as undeclared and are hidden as soon as you set a minimum. Tick “Keep undeclared” if you would rather keep them.",
                    "Worth knowing: results are ordered by how well they match your search, not by audio quality, so without the filter a YouTube result with the exact title will happily outrank three genuine FLACs.",
                ]
            },
            {
                heading: "Relevance and audio quality now have separate name badges",
                items: [
                    "The score that orders search results measures artist/title match, views, duration and a source bonus. It is now called relevance_score throughout the app and API instead of the misleading quality_score.",
                    "Actual audio quality remains quality_tier, which drives the minimum-quality filter. Search order and filtering behaviour have not changed; only the contract finally says what each number means. API clients reading search results should switch to relevance_score.",
                ]
            },
            {
                heading: "The right album, not just a plausible one",
                items: [
                    "MusicBrainz scores search matches on string similarity alone, so every recording of a popular song ties on 100 and the order between them is arbitrary. Asking it for one result was drawing a raffle ticket, which is how “Around the World” kept arriving on the live album Alive 2006.",
                    "MusicGrabber now asks for a shortlist, asks again with a studio-album filter, and works out which recording is the canonical take before picking a release from it. Compilations, DJ mixes, live bootlegs and “(Boombox Rehearsals)” takes now lose on purpose.",
                    "Measured against a 31-track test corpus spanning bootleg-heavy artists, dance compilations and non-English releases: the old behaviour got 3.7 of 31 right, the new one gets 26.",
                ]
            },
            {
                heading: "Manual picks can spot a suspiciously short delivery",
                items: [
                    "Choosing a live version, remix, edit or extended mix still overrides MusicBrainz's canonical duration on purpose. Your click remains the decision, not merely a polite suggestion.",
                    "MusicGrabber now remembers the duration displayed on that exact result. If a provider advertises a four-minute track but delivers a perfectly playable 30-second CDN sample, the download fails honestly instead of filing the preview in your library as mission accomplished.",
                    "Only substantial shortfalls are rejected, and incomplete manual picks are not silently replaced with a different version. Results whose provider supplied no duration retain the existing integrity checks.",
                ]
            },
            {
                heading: "Non-English tracks, searched the way you actually type them",
                items: [
                    "Searching “Yoru ni Kakeru” used to return nothing whatsoever, because MusicBrainz files that track under 夜に駆ける. Same story for “Gruppa krovi” and Группа крови.",
                    "When a search comes back empty we now try again against aliases, which is where transliterations are kept, and the track comes back properly tagged in its native script.",
                ]
            },
            {
                heading: "Track numbers on singles, at last",
                items: [
                    "Downloaded singles were arriving without a track number even when MusicBrainz knew perfectly well which track it was. Two separate causes, both fixed: the MusicBrainz search API nests the matched track under a differently-named key than the lookup API, and the fingerprint path was requesting releases without their track listings.",
                    "Vinyl pressings that print “A1” rather than “3” now fall back to the position on the disc. Reported by RxBrad.",
                ]
            },
            {
                heading: "One space too many in collaboration credits",
                items: [
                    "MusicBrainz returns each credited artist with its separator already attached (“ & ”, “ feat. ”), and we were adding a space on top, so “Underworld & Iggy Pop” went into the tag as “Underworld &  Iggy Pop”. Five places in the metadata code were guilty. Solo tracks were never affected, which is exactly why nobody spotted it.",
                ]
            },
            {
                heading: "Library scans stay inside the library",
                items: [
                    "Synology, QNAP and Windows SMB shares hide deleted files in a bin inside the share itself, so the upgrades scanner, provenance audit and Stats storage figure were all counting things you deleted weeks ago, and occasionally offering to upgrade them.",
                    "All three now share one walker that skips @Recycle, #recycle, .Trash-1000, @eaDir, System Volume Information, lost+found and the macOS metadata caches, and will not follow a symlink out of your music folder. Albums actually named “Trash” remain untouched.",
                ]
            },
            {
                heading: "Monochrome downloads stopped dawdling",
                items: [
                    "A Monochrome track took 37 seconds to resolve before it even started downloading. It now takes about 0.2 seconds once the first one has gone through.",
                    "The shared Qobuz token pool has 28 tokens in it, of which exactly 3 still work, and naturally they are the last three in the list. Every download was politely working its way through 25 dead ones first. MusicGrabber now remembers which token actually delivered and starts there next time.",
                    "Two of the three default Qobuz proxies have also been retired: one answered only after burning a 20-second timeout, and the other no longer exists in DNS. Your own self-hosted proxy URLs are left exactly as you set them.",
                ]
            },
            {
                heading: "Results is now Tracks",
                items: [
                    "Same tab, same job, clearer name. Requested on reddit, and difficult to argue with.",
                ]
            },
            {
                heading: "Audio provenance, with receipts",
                items: [
                    "Watched now includes a read-only Audio Provenance Audit for the complete configured music directory: Singles, Playlists, Albums, and historical files. Every row shows the observed stored format, the acquisition history MusicGrabber actually recorded, the resulting conservative classification, and the evidence used to get there.",
                    "The report distinguishes known lossy transcodes, native lossy acquisitions, lossy derivatives of recorded lossless files, recorded lossless acquisitions, unknown history, and unreadable files. “Recorded lossless” is not presented as forensic proof; a large bitrate and a FLAC overcoat cannot testify about the file's entire childhood.",
                    "Filters cover classification, container, codec, acquisition source, and effective quality. The two lossless views are deliberately separate: stored in a lossless codec versus recorded as a lossless acquisition.",
                    "Filtered CSV and JSON dry-run reports include the scan criteria, evidence, caveats, and an explicit notice that no audio was changed. The audit has no rename, retag, move, delete, replace, or upgrade buttons.",
                    "Large audits publish atomically, recover interrupted runs honestly, and pause for interactive library views instead of turning your NAS and the rest of the UI into treacle.",
                ]
            },
            {
                heading: "Featured artists reach the same verdict",
                items: [
                    "Local files, Navidrome, Lidarr, and watched playlists now agree when one service credits a guest artist and another files the track under the primary artist. A track such as “The Chemical Brothers, Q-Tip – Go” can match “The Chemical Brothers – Go”, and a later duplicate download result repairs the watched Missing row instead of leaving it there for decorative purposes.",
                ]
            }
        ]
    },
    "3.0.3": {
        title: "What's New in v3.0.3",
        sections: [
            {
                heading: "Upgrades show their workings",
                items: [
                    "Watched Upgrades now separates the file stored on disk from the audio it came from. Instead of showing only “FLAC 622kbps”, a known transcode can say “FLAC 622kbps stored · source MP3 128kbps · effective 128 tier”, making it clear why a genuine lossless result is an upgrade.",
                    "Quality decisions now use MusicGrabber's structured source codec and bitrate tags before falling back to older human-readable labels. A genuinely lossless source is not offered another lossless wrapper, while a lossy source converted to FLAC remains honestly eligible.",
                    "Lossy-to-lossy conversion no longer inflates the effective tier either: quality is capped by both the original and stored encodings, because neither side can restore information the other discarded.",
                ]
            },
            {
                heading: "Watched playlists repair their history",
                items: [
                    "Append-mode playlists now check their full retained history when they refresh, not only the tracks still present in today's source playlist. If an older chart song has since appeared in MusicGrabber's library, Navidrome, or Lidarr, its watched status and real file path are repaired automatically and it joins the rebuilt M3U.",
                    "Search and watched playlists now agree about Lidarr-managed music. The awkward case where Search said “already in your library” while the watched playlist insisted the same song was missing has been fixed.",
                    "Songs that have left the upstream playlist are never re-downloaded during this repair. Genuine historical misses are remembered and checked periodically, daily by default, so pressing Refresh does not repeatedly send the same expensive queries to Navidrome and Lidarr.",
                    "The database migration is automatic. The first refresh after upgrading performs the historical repair pass; later refreshes retain the healed paths and use them directly.",
                ]
            },
            {
                heading: "A livelier Queue",
                items: [
                    "The Queue now has a live workbench summary showing how many tracks are downloading and how many are waiting. Active jobs show their current stage and a second-by-second elapsed time, while a live-check indicator confirms the page is still keeping watch. Tiny hammers may be involved.",
                    "Queue updates arrive faster while downloads are moving, then ease off once everything is finished. Elapsed timers keep ticking locally, so the display stays lively without asking the server for an update every second.",
                    "Loading the Queue now says it is taking attendance, an empty Queue is suspiciously quiet, and Search changes its button and loading message while the search party warms up. Waiting should at least have decent patter.",
                ]
            },
            {
                heading: "Small but useful",
                items: [
                    "Audio controls now say either “Keep source” or “Convert to [format]” instead of expecting a small unlabeled switch to explain the metaphysics of transcoding. Fresh installations keep the provider's format, existing preferences stay put, and Settings makes clear that wrapping lossy audio in FLAC or ALAC cannot bring the missing bits back from the dead.",
                    "Long notifications now stay visible long enough to read, and a new notification cannot be accidentally dismissed by the timer left behind by the previous one.",
                    "Search progress, Queue updates and notifications are announced more clearly by screen readers, while reduced-motion mode now quietens spinners, pulsing status dots and toast movement.",
                    "Queue progress timestamps are now handled consistently as UTC, avoiding misleading times when the browser and server live in different time zones.",
                    "The source-health chips now keep a comfortable gap beneath the search box instead of clinging to its bottom edge.",
                    "Opening Albums after a successful track search now carries the artist across and immediately looks up the artist choices again. Related-search chips and other Results-only controls also stay on Results instead of following you into the Albums page.",
                ]
            }
        ]
    },
    "3.0.2": {
        title: "What's New in v3.0.2",
        sections: [
            {
                heading: "Search and source health",
                items: [
                    "A new source-health strip shows which enabled services are available before you search. It updates while the page is open, explains failures in a tooltip, remembers parked services across restarts, and lets admins start a fresh check without visiting the logs.",
                    "If a source times out or errors during a search, its status row now offers Retry this source. Recovered matches are merged into the results already on screen, so the successful providers do not have to rummage through their catalogues again.",
                    "One enabled source can now fill all 15 result slots, while two can contribute eight apiece. The usual per-source caps still protect larger searches from being monopolised by one enthusiastic provider.",
                    "Bulk imports and watched refreshes now reuse identical recent searches for 15 minutes. Empty failures are never remembered, and blacklist changes clear the cache immediately.",
                    "Slow providers can no longer leave a growing trail of abandoned searches. Each service admits one live call at a time, Soulseek avoids its second empty-result attempt when other sources are running, and Monochrome's hifi lookup now shares a 15-second budget across all of its endpoints.",
                    "Monochrome hover previews now follow the same recovery route as downloads while staying strictly lossless. They can recover a bad ISRC through Deezer, use Tidal's lossless stream for Tidal-backed results, and forget stale CDN links instead of repeatedly replaying a dead URL.",
                ]
            },
            {
                heading: "Queue",
                items: [
                    "Expanded Queue details now retain the complete source journey when fallback hops between providers, so MONO to SLK to YT is visible instead of being overwritten by the final attempt.",
                ]
            }
        ]
    },
    "3.0.1": {
        title: "What's New in v3.0.1",
        sections: [
            {
                heading: "Fixed",
                items: [
                    "Searches no longer sit in silence for up to half a minute when a source's health check has gone stale. Health probes now run quietly in the background instead of making your search wait for them, so results start streaming in straight away even when Monochrome's proxies are having one of their days.",
                    "When Monochrome's proxies are down, downloads now give up on them in about a minute and move on to another source, instead of grinding through every proxy, quality tier and retry round for up to a quarter of an hour first. Your track arrives from elsewhere while the proxies sulk in peace.",
                    "A source that keeps timing out during searches now gets benched after three strikes rather than slowing every search (and every track of a bulk import) to a crawl until its next scheduled check-up.",
                    "Slow-but-alive downloads are no longer declared dead at 15 minutes flat. Jobs now have a proper heartbeat, so the timeout only fires when a download has genuinely stopped making progress, not just because a big FLAC was taking the scenic route.",
                    "Blacklisting a bad Soulseek file now actually sticks. Results used to get a new random identity every search, so the same dodgy file could waltz back in wearing a fresh moustache; it's now recognised on sight.",
                    "The zvu4no MP3 search returns results again. The site quietly moved house (zvu4no.org became zvu4it.org) and our search was left knocking at the old door, so every result got filtered out. We now follow it wherever it lands, and we've made the fix rebrand-proof so the next moonlight flit doesn't catch us out.",
                    "FreeMp3Cloud search is back too. It had also shuffled to a new address, and our search kept landing on the plain front page instead of the actual results. Now it follows the move properly, so tracks turn up again.",
                ]
            },
            {
                heading: "Removed",
                items: [
                    "MP3Phoenix has been retired as a search source. It put up a full Cloudflare 'Just a moment...' wall that we couldn't get a download through, despite a proper scrap (three different bypass tools, all on a clean residential connection). Rather than build an entire browser just to chase one lossy 320kbps source, we've let it go. It had a good run, mostly setting the fire alarm off.",
                ]
            }
        ]
    },
    "3.0.0": {
        title: "What's New in v3.0.0",
        sections: [
            {
                heading: "Added",
                items: [
                    "Live search progress. Instead of one spinner that leaves you guessing, results now stream in as each source answers, with the best floated to the top, and a wee panel ticks each source off live: searching, done with a count, offline, or too slow. Each row gently fades away as its source finishes, the status lines are cheeky but honest (with a whole section of funny failure lines), and Soulseek now appears in the same flow as every other source (turn it on under Search Sources).",
                    "New 'Normalise Loudness' toggle in Settings (off by default). YouTube downloads range from whisper-quiet to deafening; turn this on and every download from a lossy web source is levelled to the same streaming-standard volume as it arrives. It's a pure gain adjustment (no compression, no pumping), cover art survives, and lossless sources like Monochrome and Soulseek are never touched. Thanks to Upstairs-Brush for the suggestion.",
                    "New 'Auto-Import Copy Folder' setting (File Organisation). Point it at a mounted macOS Music 'Automatically Add to Music' folder and every finished download drops a copy in there, so new tracks appear in Music by themselves. Your library keeps its own file; Music consumes the copy. Another smailpouri special.",
                    "New 'Find Orphans' button in the Watched tab. Mirror-mode removals leave audio files behind in playlist folders (we don't delete music); this rounds up the leftovers and lets you move them, one by one or all at once, into your normal Singles layout, lyrics included. Nothing moves without you clicking.",
                    "Singles searches now warn when Navidrome or Lidarr already knows about the track. It is a heads-up, not a hard stop; you can still download another copy if you are deliberately hoarding bangers.",
                ]
            },
            {
                heading: "Fixed",
                items: [
                    "Monochrome now finds tracks that Deezer's own search likes to hide. It searches Deezer by artist and title fields directly (and tries both orderings, in case you typed the title and artist the wrong way round), so an exact match that used to be buried under fuzzy near-misses now turns up at the top.",
                    "When Deezer only returns a page of weak guesses, Monochrome now also checks Tidal instead of stopping early, so a track that lives on Tidal but not Deezer can still be found.",
                    "Monochrome searches are fast again when its Qobuz proxies are having an outage. Dead proxies are skipped instead of being retried one by one, so a search that could previously stall for the best part of a minute (long enough to be dropped entirely) now returns in well under a second and its lossless results actually show up.",
                    "Searches wait a little longer (up to 30 seconds) before giving up on a slow source, so a momentarily sluggish lossless source isn't dropped prematurely.",
                    "Restoring a wrongly-binned track now re-applies its playlist Comment tag straight away, rather than waiting for the next manual playlist refresh.",
                    "Tracks skipped as duplicates during a playlist refresh now get their playlist Comment tag too, so macOS Music smart playlists see every member, not just the freshly downloaded ones.",
                ]
            }
        ]
    },
    "2.9.5": {
        title: "What's New in v2.9.5",
        sections: [
            {
                heading: "Added",
                items: [
                    "New 'Group playlists as one compilation' toggle (Settings, File Organisation). Turn it on and every track that lands in a playlist folder is tagged with the playlist name as its album, 'Various Artists' as the album artist, and the compilation flag, so Plex and Navidrome group the whole playlist together instead of inventing a separate one-track album for each song. Tracks collapsed this way also drop their original studio-album track number, so the playlist isn't reordered by stray '8 of 13' positions. Off by default; ideal for Shazam-style mixed playlists.",
                    "New 'Deezer Album Fallback' toggle (Settings, General; on by default). MusicBrainz is slow to catalogue brand-new releases, so newly released singles often arrived with no album and stayed stuck in Singles/ even with auto-album routing on. Now, when MusicBrainz can't find the album, Deezer is asked instead and fills in the album, year and track number so the track gets filed properly. It only adds what MusicBrainz left blank, and only when it's confident it's the same studio track, so your existing tags are never overwritten.",
                    "Album downloads now grab the proper studio recording instead of occasionally pulling a live version. The Albums tab uses each track's MusicBrainz ISRC (which identifies one specific recording) to ask Monochrome for that exact cut from Qobuz or Tidal, so a concert take can't slip into your studio album. If a track isn't stocked that way it falls back to the normal artist-and-title search as before. Songs that simply have 'live' in the name, like 'Live and Let Die', are never affected.",
                    "New 'Reject live versions' toggle in Settings (off by default). Turn it on and any track that turns out to be a live recording you didn't ask for is binned and retried for a studio version. It only acts when it can confidently identify the track as live, so it never bins anything by mistake, and songs that just have 'live' in the title (like 'Live and Let Die') are never touched.",
                    "New 'Tag playlist names in Comment' toggle in Settings (off by default). Writes the watched-playlist name(s) a track belongs to into its Comment tag so macOS Music can build smart playlists from them; a track on several playlists gets all their names. New downloads are tagged automatically and there's a one-shot backfill to tag your existing library. With thanks to smailpouri for the suggestion.",
                ]
            },
            {
                heading: "Improved",
                items: [
                    "The 'Fallback' setting (Settings, Sources) got a lot more trustworthy. When a download fails and it re-routes to another source, the replacement now has to pass a same-track confidence check first, so it won't quietly swap in a live version, a remix, or an entirely different song just because it scored well.",
                    "Fallback now also kicks in for YouTube and SoundCloud, not just Monochrome and the MP3 sources, and once a source has been marked offline mid-attempt it stays out of the running so the chain doesn't keep retrying a platform that's clearly having a bad day.",
                ]
            },
            {
                heading: "Fixed",
                items: [
                    "Searches no longer hang for the best part of a minute when one source is being slow. A multi-source search used to run only as fast as its slowest source, so a single sluggish site could leave the whole search looking dead. There's now a hard 15-second ceiling: results from the sources that answered in time come straight back, and anything still dragging its heels is simply left behind. Healthy searches are still near-instant.",
                ]
            }
        ]
    },
    "2.9.4": {
        title: "What's New in v2.9.4",
        sections: [
            {
                heading: "Added",
                items: [
                    "Monochrome search got a brain transplant. It now asks Deezer first, which copes with misspelt artists and titles, points straight at the studio version instead of the live-at-somewhere one, and every result shown is pre-checked as actually downloadable from Qobuz at the quality it claims. The old route still backs it up, so nothing is lost on the rare track Deezer doesn't know.",
                    "Tracks that used to fail with 'no Qobuz stream available' get two new escape routes: if the track ID we were given is wrong or mangled (Tidal occasionally ships ISRCs that fail a basic format check), Deezer is asked for the correct one and the download retried; and if Qobuz genuinely doesn't stock the track at all, it's now pulled from Tidal's own stream as a last resort, in proper lossless FLAC.",
                ]
            }
        ]
    },
    "2.9.3": {
        title: "What's New in v2.9.3",
        sections: [
            {
                heading: "Added",
                items: [
                    "Everyone gets their own audio format. The FLAC / ALAC / Opus / MP3 picker in Settings is no longer admins-only: each user can now choose their own format (and bitrate) and their downloads convert to it. Anyone who hasn't picked carries on with the admin's default, setting AUDIO_FORMAT in docker-compose still locks it for the whole house, and Peons still take what they're given.",
                ]
            }
        ]
    },
    "2.9.2": {
        title: "What's New in v2.9.2",
        sections: [
            {
                heading: "Added",
                items: [
                    "Monochrome no longer goes dark for searching when monochrome.tf has one of its turns. If the usual search route is down, MusicGrabber now asks Qobuz directly (through the same fallback it already uses for downloads) so you still get real lossless results. When monochrome.tf is up you get the full catalogue and hi-res exactly as before; the fallback only kicks in when the normal search comes back empty.",
                ]
            },
            {
                heading: "Fixed",
                items: [
                    "Large Spotify playlists no longer come back capped at 100 tracks. Spotify's quick embed only gives the first 100, so bigger playlists use a headless browser to scroll the rest, but a recent Spotify change was making that time out on really big lists and quietly fall back to just the 100. It now reads the real track count off the page, gives itself enough time to scroll the whole thing, and if a genuinely enormous playlist still runs long it hands back everything it managed to grab (with a heads-up) rather than dropping back to 100.",
                    "Tracks with monstrously long titles (the 30-artist remix with everyone and their cousin on the feature list) no longer crash with a 'File name too long' error and take the rest of the playlist down with them. The filename is now trimmed to fit your filesystem's limit, accented and non-Latin titles are cut cleanly without mangling a character, and a track you've already got is still recognised so it isn't downloaded twice.",
                ]
            }
        ]
    },
    "2.9.1": {
        title: "What's New in v2.9.1",
        sections: [
            {
                heading: "Added",
                items: [
                    "New search source: FreeMp3Cloud. It's quality-aware, so the tracks it flags as 'HQ' (real 320 kbps) are scored right up with MP3Phoenix, while its 128 kbps results sit at the bottom and only turn up when nothing better does. On by default; toggle it in Settings under Search Sources. Lossless still always wins.",
                    "Track Upgrades: a new opt-in feature for holding out for a better copy, Lidarr style. Accept a track now at whatever quality you can get, and MusicGrabber will quietly notice when something better turns up and offer to swap it in. It's off by default; turn it on in Settings.",
                    "New 'Watched Upgrades' section under the Watched tab. It lists the files MusicGrabber downloaded that are sitting below the quality you normally download at (Singles and playlist tracks only, your Albums are never touched), and as you browse it searches each one for a better copy, showing the proposed source, quality, and match confidence. Verified sources like Monochrome say so; Soulseek results are honestly badged 'needs download to confirm'. Hover any proposal to preview it before you commit.",
                    "Hit Upgrade (or Upgrade All) and MusicGrabber downloads the better copy to one side, checks it really is better and really is the same recording (duration, title/artist match, and an acoustic fingerprint), then moves your old file to a quarantine folder and drops the new one in its place. Tags and playlist entries are fixed up and Navidrome is told to rescan. If anything looks off, the download is binned and your file is left alone.",
                    "The old file always goes to quarantine rather than the bin, so any upgrade is reversible. Quarantine is never auto-emptied; that's your call.",
                    "Force anyway: if an upgrade gets knocked back because the proposed copy looks like a different recording (say you'd rather have the studio cut than your six-minute live rip), you can override it. It still quarantines the old file, so you can always change your mind.",
                ]
            },
            {
                heading: "Fixed",
                items: [
                    "A track no longer fails to download just because YouTube hiccupped on the cover art. If the thumbnail can't be fetched, MusicGrabber now keeps the audio (which had already downloaded and tagged fine) instead of binning the whole thing; you just won't get embedded artwork on those rare occasions.",
                    "Spotify playlist imports no longer fall over when Spotify has a momentary wobble. Their embed servers throw the odd gateway timeout, and one of those used to fail the whole import; now it quietly retries a couple of times first, so a blip sorts itself out instead of bringing the playlist down with it.",
                ]
            }
        ]
    },
    "2.9.0": {
        title: "What's New in v2.9.0",
        sections: [
            {
                heading: "Added",
                items: [
                    "MusicGrabber now parks sources that are down instead of showing results that cannot preview or download. It checks source health at startup and during multi-source searches, hides parked sources for a configurable cooldown, and shows a toast when results were skipped.",
                    "If a source goes offline mid-download (Monochrome's proxies love doing this), MusicGrabber now automatically retries the track on another source instead of failing. The dead source is skipped so it can't keep handing you its own broken results. There's a new 'Fall back across sources' toggle in Settings (on by default) if you'd rather a job fail loudly than quietly grab a lower-quality copy from elsewhere.",
                    "Monochrome has a new direct qbdlx fallback for the days when every public Qobuz proxy is down. It uses the shared qbdlx free-account token pool to ask Qobuz for the stream directly, so Monochrome can still deliver real lossless FLACs even when the proxy layer is having a bad day. The fallback can be turned off in Settings.",
                    "Watched playlists that vanish upstream now get paused with an explanation, instead of quietly failing forever. If a playlist is deleted or made private, its refresh keeps returning 'not found'; after three of those in a row, MusicGrabber pauses (not deletes) the playlist and adds a note to the card so you know to check the source.",
                    "We deliberately wait for a few strikes before pausing, because a private playlist with an expired login token can also return 'not found' and we'd rather not pause a healthy playlist over a one-off blip. Hit Resume once you've sorted the source out, and the slate is wiped clean.",
                    "Watched artists with huge back-catalogues are now paginated. The tracks view shows 50 at a time with Prev/Next, so following someone like Radiohead no longer paints a giant wall of singles.",
                ]
            },
            {
                heading: "Changed",
                items: [
                    "Monochrome now tries its flaky Qobuz proxies several times (sweep all, wait a beat, repeat) before giving up and falling back to another source. A proxy that's down for a couple of seconds no longer kills your download. Tracks that genuinely aren't on Qobuz still fail fast, so you're not left waiting for bad news.",
                    "Adding or refreshing a watched artist no longer freezes the app. Seeding a prolific artist's catalogue from MusicBrainz used to hold the database's write lock for minutes, blocking everything (even login) until it gave up with 'database is locked'. Seeding now runs in the background in small cycles, the 'Add artist' button returns instantly, and the card shows live progress.",
                ]
            },
            {
                heading: "Fixed",
                items: [
                    "Monochrome hover previews now use the qbdlx fallback too. Source health could correctly say Monochrome was up because qbdlx could stream, while preview still failed because it only knew about the dead proxy path.",
                    "The Queue tab should load faster and more reliably on large or slow music mounts. Queue refreshes no longer walk the library to verify every completed file; the background reconciler handles that slower housekeeping.",
                    "Queue polling is gentler on SQLite. Session 'last seen' updates are throttled, which avoids turning every authenticated refresh into a database write and reduces intermittent 'database is locked' failures.",
                    "A dead or mistyped YouTube playlist URL now returns a clear 'not found' instead of a Bad Gateway that looked like the server had crashed.",
                ]
            }
        ]
    },
    "2.8.18": {
        title: "What's New in v2.8.18",
        sections: [
            {
                heading: "Fixed",
                items: [
                    "Watched artists no longer re-download old tracks on every refresh: tracks released before an artist's start date were being mistaken for failed downloads and re-queued endlessly. They now stay in the past where they belong.",
                    "Monochrome broken on fresh installs: two separate bugs conspired: the source was silently disabled by default (wrong fallback in the code), and the Qobuz proxy (qdl-api.monochrome.tf) had its credentials expire so every download failed with a 400. Both fixed.",
                    "Monochrome Qobuz proxy is now a fallback list, just like the hifi-api endpoints. Two working community proxies (qobuz.kennyy.com.br, mono.scavengerfurs.net) are tried first. Existing installs are migrated automatically (DB migration v7).",
                    "Proxy health checking: a background thread probes all Qobuz proxies once per hour. A 4xx/5xx marks a proxy as deprioritised for 30 minutes; it recovers automatically if it starts working again. Connection errors don't blacklist.",
                    "Monochrome was missing from the Stats tab Sources breakdown; it was never wired up to the bar or legend. It now appears in teal.",
                ]
            }
        ]
    },
    "2.8.17": {
        title: "What's New in v2.8.17",
        sections: [
            {
                heading: "Fixed",
                items: [
                    "Monochrome endpoint hotfix: the entire qqdl.site cluster and both apex instances went down simultaneously (as Monochrome infrastructure tends to do). The new primary is us-west.monochrome.tf, which is proper HTTPS with no redirect shenanigans. Existing installs are migrated automatically.",
                ]
            },
        ]
    },
    "2.8.16": {
        title: "What's New in v2.8.16",
        sections: [
            {
                heading: "Added",
                items: [
                    "The release-notes pop-up now has a centered 'Donate a coffee' button at the bottom, using the same green primary-button style as the rest of MusicGrabber. Gotta get the kid some new flipflops for summer some how :)",
                    "Bulk Import and Watched Playlists now accept public Monochrome playlist links, including `monochrome.tf/playlist/...` shares.",
                ]
            },
            {
                heading: "Fixed",
                items: [
                    "Bulk Import's Preferred source dropdown now populates on app startup and when the Bulk tab opens. It no longer waits for the Watched tab to load source chips first.",
                    "M3U playlist names now have a non-empty fallback. If a playlist name is just slashes or other filename-hostile characters, MusicGrabber falls back to a stable import URL/playlist ID label instead of creating `.m3u`.",
                    "Bulk Import's Preferred source dropdown now acts as the source choice for that import. If you pick Monochrome, the run stays on Monochrome instead of falling back to YouTube or MP3Phoenix when another source scores.",
                    "Monochrome bulk imports now handle Spotify's punctuation-heavy track strings. Queries like `Artist1, Artist2 - Track Name` are searched exactly as shown and with punctuation softened to spaces, then the Tidal results are deduped before scoring. If the exact query gets a 503 from hifi-api, the cleaned query is still tried.",
                    "The default Monochrome hifi-api setting now supports a comma-or-newline separated fallback list. Existing installs still using the suspended `api.monochrome.tf` host are migrated to a list headed by `monochrome-api.samidy.com`, and MusicGrabber caches the first endpoint that answers during a run.",
                ]
            },
        ]
    },
    "2.8.15": {
        title: "What's New in v2.8.15",
        sections: [
            {
                heading: "Looks Different",
                items: [
                    "Full CSS overhaul. The app now uses self-hosted Elms Sans and SUSE Mono (no Google Fonts call, no first-paint delay, works offline). Buttons across every tab share one design system: primary actions are filled green, secondary actions are outlined, destructive actions go red on hover, warning actions stay amber. The Search, Bulk Import, Watched, ListenBrainz, and Watched Artists boxes all use the same inside-button search-pill layout at the same size, instead of the three different patterns we had before. Native checkboxes and radio buttons are now accent green instead of browser blue. Same features, more consistent surface.",
                ]
            },
            {
                heading: "Added",
                items: [
                    "Preferred source dropdown for Bulk Import and Watched Playlists. Pick a single source (Soulseek, YouTube, whatever) and it gets a huge score boost so it wins nearly every close call. Useful when you want lossless from Soulseek as primary and YouTube only as a fallback. The strict-artist guardrails still get to reject a clearly-wrong match, so you will not get a karaoke cover just because Soulseek had one. Leave it on 'No preference' to keep the existing best-quality-wins behaviour. Requested by Max S.",
                    "Albums tab now retries MusicBrainz lookups on timeout (up to three attempts with a gentle backoff) and shows a Retry button when it gives up, instead of pretending the artist or album does not exist. A partial album list is kept if MusicBrainz dies mid-pagination on prolific artists.",
                ]
            },
            {
                heading: "Fixed",
                items: [
                    "'Add to playlist' now actually adds the track to the playlist. Two separate bugs were teaming up: retrying a missing track from a watched playlist marked it as downloaded but never rewrote the .m3u file on disk, and using 'Add to playlist' from the Results tab silently no-op'd if you had not configured a Playlists folder. Both paths now do the right thing: watched-playlist M3Us get rebuilt the moment a track lands, and ad-hoc playlists fall back to writing the .m3u in the Singles folder if no Playlists folder is set. Reported by Aryan Ovalekar.",
                ]
            },
        ]
    },
    "2.8.14": {
        title: "What's New in v2.8.14",
        sections: [
            {
                heading: "Fixed",
                items: [
                    "Monochrome works again, take 47. The hifi-api default was pinned to a specific Render node (eu-central.monochrome.tf) and the owner has suspended it, which broke Tidal search, Monochrome previews and Tidal playlist fetching. We have switched to the CDN-routed apex (api.monochrome.tf) which picks a healthy node automatically. Existing installs are auto-migrated; if you have a custom self-hosted hifi-api set, it is left alone.",
                ]
            },
        ]
    },
    "2.8.13": {
        title: "What's New in v2.8.13",
        sections: [
            {
                heading: "Fixed",
                items: [
                    "Beatport playlist folders are now named sensibly. The Top 100 page was using Beatport's SEO tagline as the folder name. It now reads 'Beatport Top 100', genre charts read 'Techno Top 100' and so on, and named charts still use the actual chart name.",
                    "Monochrome downloads and previews work again. The Qobuz proxy we relied on (qobuz.kennyy.com.br) went dark, so we have switched to the new official one (qdl-api.monochrome.tf). Same API, same Akamai CDN, same FLACs. Existing installs are auto-migrated, the URL stays editable in Settings if you want to point at your own.",
                    "Bulk Import's playlist URL field now accepts Beatport and Tidal links. Watched Playlists already did; the Bulk Import paste box was running a stricter allow-list and bouncing them with 'Unsupported URL'. Fixed.",
                ]
            },
        ]
    },
    "2.8.12": {
        title: "What's New in v2.8.12",
        sections: [
            {
                heading: "Added",
                items: [
                    "Beatport playlists are here. Top 100, genre charts, editorial charts; paste a Beatport URL into Watched Playlists and it pulls the track list straight from the page. Shiny vinyl icon included.",
                ]
            },
            {
                heading: "Fixed",
                items: [
                    "Tidal share links (tidal.com/playlist/UUID) were being rejected as unrecognised. The URL matcher now accepts both the /browse/playlist/ and bare /playlist/ forms.",
                    "Tidal playlist fetching actually works again. The old Monochrome-proxy approach was dead, so it has been replaced with a direct scrape of Tidal's embed player, which server-renders the full track list. Tidal now lives in its own tidal.py module.",
                ]
            },
        ]
    },
    "2.8.11": {
        title: "What's New in v2.8.11",
        sections: [
            {
                heading: "Fixed",
                items: [
                    "Monochrome source is now enabled by default again. A regression had flipped it back to off, so fresh installs were only searching YouTube, SoundCloud, MP3Phoenix, and zvu4no until someone found the toggle.",
                    "Monochrome and Qobuz proxy URLs are now correctly set for users who skip versions. If you had Monochrome before it was temporarily removed in v2.6.6 and then jumped straight to v2.8.x, those URL fields would be blank in the database and Monochrome searches would silently fail. A one-time migration now seeds the correct defaults for affected installs.",
                ]
            },
        ]
    },
    "2.8.10": {
        title: "What's New in v2.8.10",
        sections: [
            {
                heading: "Fixed",
                items: [
                    "Monochrome search now picks the canonical studio version, not the piano cover or movie soundtrack. The old scoring handed a hefty +210 bonus to anything tagged HI_RES_LOSSLESS, so a piano-tribute album in hi-res could outrank the actual Nirvana master sitting at plain lossless. The hi-res bonus has been trimmed to a 15-point edge over lossless so it can still break ties, but cannot bulldoze relevance.",
                    "Monochrome scoring now reads the Tidal track's `version` field (Live, Live Aid, Muppet version, Devonshire Mix, Boombox Rehearsals, Originally Performed by, etc.) and applies a penalty when it is set. A bare `Remastered 2011` style annotation is treated as neutral, because Tidal almost never carries the un-remastered original master.",
                    "Monochrome scoring also pulls Tidal's own popularity score (0 to 100) and adds it as a small tiebreaker, up to +10. When several copies of a track tie on quality and relevance, the one everyone actually streams wins.",
                    "Album-title penalties added for soundtrack, live, compilation, karaoke, and tribute releases. The studio album rises to the top; the soundtrack tie-in and the karaoke disc fall down the list.",
                    "Every penalty has a query-aware waiver. If you actually want the Spawn soundtrack version, search `spawn soundtrack trip like i do` and the Filter collab comes out on top. Search `crystal method - trip like i do` with no soundtrack hint and you get the Vegas studio cut.",
                ]
            },
        ]
    },
    "2.8.9": {
        title: "What's New in v2.8.9",
        sections: [
            {
                heading: "Fixed",
                items: [
                    "Album-routed filenames now use the album artist instead of every credited track artist. A track like Despacito (Remix) can still keep Luis Fonsi, Daddy Yankee, and Justin Bieber in the metadata, but the album folder/flat filename prefix uses Luis Fonsi when MusicBrainz provides that as the album artist. This keeps Lidarr-style album layouts tidy without throwing away featured-artist credits.",
                ]
            },
        ]
    },
    "2.8.8": {
        title: "What's New in v2.8.8",
        sections: [
            {
                heading: "Added",
                items: [
                    "End-to-end QA test that drives the whole acquisition flow in one go: pull a single, pull a small album, import a 3-track Spotify playlist, add a watched playlist, manually trigger its scheduled refresh, then re-run the lot with Navidrome and Lidarr dupe checks toggled back on to prove the library dupe check is actually catching them at every layer. Cleans up after itself. Lives behind the slow marker so the fast test suite stays fast. Verified end to end in 3m26s against the test VM.",
                    "dupe_skipped count on the bulk import status endpoint: counts jobs that completed via the 'Already exists' short-circuit, separately from fresh downloads. Until now there was no API-visible way to tell whether the library dupe check had actually engaged.",
                ]
            },
            {
                heading: "Fixed",
                items: [
                    "Watched playlists were rejecting every add with a 500 on older databases: the v1 multi-user migration recreated the watched_playlists table from a hard-coded schema that quietly forgot the custom_subdir column, so any DB that came through that migration blew up with 'table watched_playlists has no column named custom_subdir'. The migration now keeps the column on the way through, and a defensive ALTER TABLE runs after the migration so any database that already lost it gets it back on next start.",
                    "Second Spotify fallback was still gated behind sp_dc: the v2.8.8 fix that dropped the cookie requirement on the 95+ track browser fallback missed its sibling code path. When the embed parser returned zero tracks (small public playlists), the code only tried the headless browser if sp_dc was set. Gate removed.",
                    "Watched playlists were re-downloading every track on re-add: _has_local_track_file was filtering Navidrome's dupe-check result by .is_absolute(), which threw away the sentinel return that means 'exists but Navidrome real-path mode is off'. The refresher then treated every track as missing and queued the lot again. Sentinel is now honoured the way it was always supposed to be.",
                    "SoundCloud info-lookup errors were uniformly opaque: yt-dlp could fail for half a dozen reasons and every one rendered as 'SoundCloud info lookup failed: provider rejected the request'. New patterns for 404/410/geo/DRM/private-track land, the first real ERROR line from yt-dlp is surfaced as a fallback, and the raw stderr now lands in the container log so future SoundCloud breakage is diagnosable.",
                    "Watched playlist add now surfaces the actual server error instead of a cryptic 'Unexpected token I is not valid JSON'. The frontend was trying to parse a plain-text 500 body as JSON; it now checks Content-Type first and shows the real status and message.",
                    "Spotify private and embed-blocked playlists now fall back to the headless browser for both watched playlists and bulk imports. Previously the embed scraper bailed on a 401/403 or zero-track result; it now tries the Playwright path with your sp_dc cookie before giving up, which is the same fallback that already handled 95+ track playlists.",
                    "Large public Spotify playlists no longer cap at 100 tracks without cookies. The 95+ track browser fallback was gated behind sp_dc; that gate is gone, so a 200-track public playlist now returns 200 tracks even with no cookies set.",
                    "Bulk Import button now updates while the headless browser is working. After a few seconds it switches to 'Spinning up browser...' and then 'Scrolling playlist...' so a 60-second wait no longer looks like a hang."
                ]
            }
        ]
    },
    "2.8.7": {
        title: "What's New in v2.8.7",
        sections: [
            {
                heading: "Apple Music private library playlists",
                items: [
                    "Pop your Music-User-Token into Settings under Apple Music and MusicGrabber can now fetch playlists from your personal library (music.apple.com/library/...). The web bearer token is still pulled automatically from Apple's public JS bundle, so only the user token needs supplying. Bulk Import also stops rejecting library URLs for not having a country code, which was an inconsistency it had been smug about for far too long.",
                ]
            },
            {
                heading: "New song matching engine for Soulseek",
                items: [
                    "Soulseek results were being scored by the YouTube-shaped scorer, which knows about channels and view counts but nothing about path segments. They are now scored by a proper path-aware confidence engine that splits the slskd filename into Artist/Album/Track and matches each piece independently. Beyoncé finds Beyonce, Pig & Dan finds Pig&Dan, Kraftwerk survives a typo as Kraftwork, and Muse no longer wins inside a folder called Museum Of Sound. Results below the new SLSKD_MATCH_CONFIDENCE_FLOOR (default 0.55, env-tunable) are dropped rather than shipped as the least-bad option, and Various Artists / VA / Unknown folders are rejected outright. Affects bulk imports and watched playlist downloads, since both share the same search pipeline.",
                ]
            },
            {
                heading: "YouTube, MP3Phoenix, and Monochrome ride the same matching engine",
                items: [
                    "The YouTube scorer's old token-overlap title/artist block has been retired in favour of a unified SequenceMatcher-based confidence calculation. Beyoncé finds Beyonce instead of taking a -12 artist mismatch for not having an accent. Don't Stop finds Dont Stop via the new core-title fast path instead of getting tripped up by an apostrophe. Stay (Remix) no longer silently merges with Stay because parens-stripping had discarded the version word before the comparison. The classic YouTube channel suffixes - Topic and VEVO are stripped before artist matching so BritneySpearsVEVO reads as Britney Spears. The regex stack for live/cover/karaoke/copyright-dodge/official/MusicBrainz-duration is preserved untouched, since those are real YouTube-specific signals worth keeping. MP3Phoenix and Monochrome benefit automatically.",
                ]
            },
            {
                heading: "Version-aware matching: original mix wins by default",
                items: [
                    "A search for Stay no longer politely settles for Stay (Live in Tokyo) or Stay (R3hab Remix). Remix, live, and acoustic suffixes drop the score below threshold so the original mix takes the top spot. Remasters get a light penalty only, since they are the same recording wearing a slightly nicer coat.",
                ]
            },
            {
                heading: "Wrong-song-by-right-artist no longer climbs the rankings",
                items: [
                    "Searching Machine Head - silver was pulling up ØUTSIDER, Circle The Drain, and other Machine Head tracks above the right Silver matches because the artist match was overpowering the title penalty (especially for Monochrome HI_RES candidates whose +120 quality bonus could outweigh a soft title mismatch). Two fixes: a title hard-gate scales confidence down proportionally when title similarity is below 0.4, and a no-shared-token guard in the fuzzy similarity check stops silver and utsider scoring 0.6 just because they happen to share s/i/e/r. Penalty bumps for poor and mismatched candidates have also been cranked up so a wrong-song match cannot beat a right-song match no matter how lossless the wrong song is.",
                ]
            },
            {
                heading: "Metal track names with Ø, Æ, Þ no longer get butchered",
                items: [
                    "ØUTSIDER was collapsing to UTSIDER and Mötley Crüe to mtley cre because NFKD doesn't decompose those characters (the diacritic is part of the glyph, not a combining mark). Added an explicit Latin-extended mapping so Ø/Æ/Œ/Þ/Ð/ß/Ł fold to ASCII the way users actually expect.",
                ]
            },
            {
                heading: "Unit tests for the matching engine",
                items: [
                    "48 unit tests covering normalisation, version detection, junk-folder rejection, path-segment matching, CJK preservation, streaming-source confidence, and the title hard-gate. Runs as part of the fast suite. Future tweaks to the scorer can no longer silently regress Don't Stop, Beyoncé, or anything in a heavy metal umlaut.",
                ]
            },
        ]
    },
    "2.8.6": {
        title: "What's New in v2.8.6",
        sections: [
            {
                heading: "Playlist album subfolders: no more redundant artist prefix in filenames",
                items: [
                    "After routing to Rock/Artist/Album/, the filename was still Artist - Title.flac instead of Title.flac. It now matches the singles convention: artist is in the folder, not repeated in the filename. Track-number filenames also drop the redundant prefix, landing as 5 - Title.flac rather than Artist - 5 - Title.flac.",
                ]
            },
        ]
    },
    "2.8.5": {
        title: "What's New in v2.8.5",
        sections: [
            {
                heading: "Playlist album routing now works for Soulseek and MP3Phoenix too",
                items: [
                    "The v2.8.4 fix for 'Auto-route to album folder' inside playlist folders only wired up YouTube downloads. Soulseek and MP3Phoenix both had the old guard that skipped the routing entirely, so tracks from those sources still landed flat in Rock/Artist - Title.flac. All three sources now route consistently.",
                ]
            },
        ]
    },
    "2.8.4": {
        title: "What's New in v2.8.4",
        sections: [
            {
                heading: "Album routing now works inside playlist folders",
                items: [
                    "If you had 'Auto-route to album folder' enabled, it was quietly doing nothing for playlist downloads. Tracks landed flat in Playlists/Name/Artist - Title.flac and ignored the setting entirely. They now land in Playlists/Name/Artist/Album/Title.flac, consistent with how singles behave. Track numbers in filenames work correctly here too, and the artist prefix is dropped from the filename since the folder structure already provides that context.",
                ]
            },
            {
                heading: "Spotify playlists work again",
                items: [
                    "Spotify added some new fields to their embed page that broke the regex we were using to pull out the track list. Every playlist was coming back with exactly one track, which is arguably a setlist but not what you asked for. We now parse the structured JSON Spotify already embed for their own frontend, which handles any nesting they throw at it. Should be considerably more resilient going forward.",
                ]
            },
            {
                heading: "Large Spotify playlists: proper warning, no more 30-second hang",
                items: [
                    "Playlists over 100 tracks trigger a headless browser fallback to fetch the rest. Without a Spotify cookie the browser was hitting a bot-detection wall, stalling for 30 seconds, and then returning a cryptic error about page structure. Now: if you have no cookie configured, the stall is skipped and you get a clear warning straight away explaining that you need to add your Spotify cookies in Settings to get the full list. The warning shows up on both the Fetch Playlist and Add Watched Playlist flows.",
                ]
            },
            {
                heading: "Headless browser sneaks past Spotify's bot detection",
                items: [
                    "Even without a Spotify cookie, the headless browser now masks the fingerprints Spotify use to identify automated browsers. Large playlists should fetch fully without needing a cookie in most cases.",
                ]
            },
        ]
    },
    "2.8.3": {
        title: "What's New in v2.8.3",
        sections: [
            {
                heading: "ALAC quality picker, MP3-style",
                items: [
                    "The Audio Format selector grows a quality row when ALAC is chosen, just like MP3 and Opus already do. 'Lossless' gives you proper Apple Lossless inside an .m4a wrapper, exactly as before. The new 320k / 256k / 192k / 128k options quietly switch the encoder to AAC inside the same .m4a, for anyone who wants iPod-friendly files but can live with lossy audio. The UI is honest that anything below Lossless is technically AAC; ALAC purists, look away.",
                ]
            },
            {
                heading: "MP3 320k actually means 320k now",
                items: [
                    "Two layered bugs were conspiring to give you ~192 kbps no matter what you picked. First, the MP3 and Opus bitrate fields had never been added to the settings API, so saving them silently did nothing and the default of V2 (~190 kbps) was used forever. Second, even when the value did sneak through, yt-dlp was misparsing '320K' and falling back to the libmp3lame default. Both fixed. Pick 320, get 320.",
                ]
            },
        ]
    },
    "2.8.2": {
        title: "What's New in v2.8.2",
        sections: [
            {
                heading: "Singles-only mode",
                items: [
                    "A new Settings toggle that hides the Albums tab outright. If you're running MusicGrabber as a singles feeder for Navidrome and you'd rather your less-technical users didn't poke at the album browser, this is for you. Auto-album routing for individual singles still works exactly as before; this just retires the dedicated Albums workflow from the UI.",
                ]
            },
            {
                heading: "Peon: the strictest user role yet",
                items: [
                    "Three roles now: Admin (full access), User (own settings, no admin functions), and the new Peon (locked-down request-only). Peons see Search, Bulk Import, Queue, Albums and Watched. No Settings, no Stats, no Clear Queue button, and no fiddling with Navidrome or Soulseek; they inherit whatever the admin set globally. Perfect for the family member who keeps 'helpfully' changing the audio format to MP3.",
                ]
            },
            {
                heading: "Clear Queue is now role-aware",
                items: [
                    "Standard users hitting Clear Queue only wipe their own completed/failed/stale jobs, leaving everyone else's queue alone. Admins keep the full nuke for when the shared queue has gone feral. Peons don't get the button, and the API turns them away if they try to reach it directly.",
                ]
            },
            {
                heading: "Settings tab tidied for non-admins",
                items: [
                    "Standard (non-admin) users now see only the bits of Settings that are actually theirs: per-user music subfolders, Navidrome/Jellyfin/Lidarr credentials, Spotify cookies, notifications, change password. The system-level rows, search sources, audio format, AcoustID key, minimum bitrate, Monochrome URLs and the new singles-only toggle, are admins-only. Backend writes were already locked down; this just stops standard users seeing knobs they couldn't actually turn.",
                ]
            },
            {
                heading: "Monochrome metadata stops guessing",
                items: [
                    "Tidal hands us a real ISRC for every Monochrome track, but the tagger was ignoring that and letting AcoustID's fingerprint pick whatever it fancied: remasters, karaoke versions, the occasional inexplicable Kylie Minogue. We now look the ISRC up on MusicBrainz directly, so tags match the track you actually asked for, with the right album and year. AcoustID and text search still take over if MusicBrainz has never heard of the recording.",
                ]
            },
        ]
    },
    "2.8.1": {
        title: "What's New in v2.8.1",
        sections: [
            {
                heading: "Track numbers in filenames",
                items: [
                    "A long-requested toggle: prefix the track number to filenames so your file manager sorts albums in the right order without arguing with you. Off by default, flip it on in Settings.",
                ]
            },
            {
                heading: "Fixed: Monochrome toggle would not stick",
                items: [
                    "The shiny new Monochrome switch from v2.8.0 forgot to introduce itself to the settings API, so it cheerfully reverted every time you reloaded the page. The Pydantic model now knows about it, along with the hifi-api and Qobuz proxy URL fields. Toggle once, stays toggled.",
                ]
            },
        ]
    },
    "2.8.0": {
        title: "What's New in v2.8.0",
        sections: [
            {
                heading: "Monochrome is back, and it brought Qobuz",
                items: [
                    "Monochrome.tf was ripped out in v2.6.6 when Tidal started banning the proxy accounts and every stream degraded to a 30-second teaser. They've come back with Qobuz as the audio backend: Tidal's catalogue for search and metadata, a Qobuz proxy for the actual bytes. The result is direct, no-nonsense FLAC from Qobuz's CDN.",
                    "Hi-res (24-bit/192 kHz) and standard FLAC (16-bit/44.1 kHz) both supported, depending on what Qobuz has for the track. Score bonuses match Soulseek's lossless weighting, so a proper master will beat a YouTube rip in the results.",
                    "Enable it via the Monochrome toggle in Search Sources. The public proxy endpoints are pre-configured. Advanced users can point the URL fields at a self-hosted hifi-api or Qobuz proxy instance.",
                ]
            },
            {
                heading: "Hover preview",
                items: [
                    "Monochrome results support the hover-to-preview feature like YouTube and SoundCloud. The server resolves the Qobuz CDN URL and hands it to the browser; audition before you commit.",
                ]
            },
            {
                heading: "Tidal playlists restored",
                items: [
                    "tidal.com/browse/playlist URLs work again in the watched playlist importer. This was dropped along with Monochrome in v2.6.6.",
                ]
            },
        ]
    },
    "2.7.1": {
        title: "What's New in v2.7.1",
        sections: [
            {
                heading: "Soulseek for watched playlists",
                items: [
                    "When Soulseek is globally enabled, the per-playlist source chips now include SLK, and watched playlist imports queue proper slskd download jobs with the username, filename, and size fields slskd actually needs.",
                    "Soulseek results now get a source-trust ranking boost, with extra lift for lossless and 24-bit files, and can contribute up to six candidates in merged searches. A well-shared FLAC should finally beat a grubby web rip with a suspiciously cheerful thumbnail.",
                ]
            },
            {
                heading: "Fixed: ListenBrainz Weekly Exploration stuck on old week",
                items: [
                    "ListenBrainz sometimes keeps the previous week's playlist visible after publishing a new one. MusicGrabber was re-resolving the URL correctly, then politely picking the stale exact title again. It now picks the newest playlist in the same family, so Weekly Exploration advances to the current week as intended.",
                ]
            },
            {
                heading: "Fixed: Soulseek download edge cases",
                items: [
                    "slskd can report a file path under the incomplete folder and then move it to completed before MusicGrabber copies it. MusicGrabber now checks the completed equivalent before failing, so downloads that finished quickly no longer die of their own success.",
                    "Some SMB and NAS mounts return a cryptic error when MusicGrabber renames a finished Soulseek file to its library name. The move now falls back to a copy-and-verify approach, so files no longer get stranded under the uploader's original track-numbered name.",
                ]
            },
        ]
    },
    "2.7.0": {
        title: "What's New in v2.7.0",
        sections: [
            {
                heading: "Soulseek downloads now work properly",
                items: [
                    "MusicGrabber now sends slskd the real file size when queueing Soulseek downloads, so transfers no longer get rejected because slskd thinks we asked for a zero-byte file. Very clever of us, only slightly late.",
                    "Completed slskd files are now found even when slskd stores them under album folders instead of the original Soulseek user path. Point MusicGrabber at the completed-downloads root and it will search from there.",
                ]
            },
            {
                heading: "Soulseek is now opt-in",
                items: [
                    "Soulseek has its own Search Sources toggle in Settings and defaults to off. slskd credentials alone no longer enable it. Turn it on in the UI or set SOURCE_SOULSEEK_ENABLED=true.",
                    "The slskd Settings panel now warns that API credentials are enough for searching, but downloads also need the completed-downloads folder mounted into MusicGrabber. Test Connection checks that path too.",
                ]
            },
        ]
    },
    "2.6.6": {
        title: "What's New in v2.6.6",
        sections: [
            {
                heading: "Removed: Monochrome/Tidal source",
                items: [
                    "Monochrome shut down on 25 April 2026. It was a genuinely excellent source, giving us real lossless FLAC straight off the Tidal CDN. All Monochrome code has been removed: the API client, mirror pool, DASH manifest fetching, Tidal playlist import, preview proxy, and the Tidal CDN cover art fallback. The MO button is gone from the search bar. YouTube, SoundCloud, MP3Phoenix, zvu4no, and Soulseek remain. RIP.",
                ]
            },
            {
                heading: "Added: zvu4no source",
                items: [
                    "zvu4no.org is now a first-class search source, shown with a ZV badge. It scrapes the Russian MP3 portal's search pages and returns direct download URLs, so preview playback and downloads happen without yt-dlp. Results go through the same scoring, integrity checking, metadata lookup, and format conversion as any other source. Toggle it in Settings under Search Sources, or set SOURCE_ZVU4NO_ENABLED=false to disable it entirely.",
                ]
            },
            {
                heading: "Fixed: ListenBrainz weekly playlists duplicating",
                items: [
                    "Re-adding a ListenBrainz username after a new week had rolled over was creating duplicate watched playlist entries instead of recognising the ones already being watched. It now also checks by username and playlist name prefix, so re-adding your username won't stack up duplicates.",
                ]
            },
            {
                heading: "Fixed: Retried downloads losing playlist folder routing",
                items: [
                    "When a download retried due to an integrity failure or duration mismatch, the custom subfolder routing was dropped, so the file ended up in the wrong place. The retry paths now carry the folder context through correctly.",
                ]
            },
        ]
    },
    "2.6.5": {
        title: "What's New in v2.6.5",
        sections: [
            {
                heading: "Fixed: Monochrome serving 30-second clips",
                items: [
                    "Some Monochrome mirror instances have degraded Tidal subscriptions and were handing out 30-second preview clips instead of full tracks. MusicGrabber now checks the trackPresentation field in every manifest response and skips any instance that returns a preview. If no instance can deliver the full track, it falls back to YouTube as usual, rather than quietly saving a clip to your library.",
                ]
            },
        ]
    },
    "2.6.4": {
        title: "What's New in v2.6.4",
        sections: [
            {
                heading: "Changed: New mirror pool",
                items: [
                    "Some more sources for high quality audio have been added.",
                    "If a Monochrome stream URL fails during download, MusicGrabber now tries a fresh stream URL from another mirror before falling back to another source.",
                ]
            },
        ]
    },
    "2.6.3": {
        title: "What's New in v2.6.3",
        sections: [
            {
                heading: "Fixed: Retried playlist jobs landing in Singles",
                items: [
                    "When a watched-playlist track was retried from the queue, or a mismatch was force-accepted, the file ended up in your Singles folder instead of the playlist folder. The next refresh would find it there and stop trying to re-download, but the M3U never included it. Routing context is now restored correctly on retry.",
                ]
            },
        ]
    },
    "2.6.2": {
        title: "What's New in v2.6.2",
        sections: [
            {
                heading: "Fixed: Album Retries Blocked by Dupe Check",
                items: [
                    "When an album track failed and was retried, it could get blocked by the duplicate checker finding the same artist/title in your Singles folder and refusing to download again. The album download intent is now stored in the database as a lock that persists through failures, so any retry skips dupe checking until the file is safely on disk, then releases the lock.",
                ]
            },
        ]
    },
    "2.6.1": {
        title: "What's New in v2.6.1",
        sections: [
            {
                heading: "MP3 and Opus Quality Settings",
                items: [
                    "When MP3 or Opus is selected as the audio format, a quality sub-row now appears in Settings. MP3 offers LAME VBR V2 (~192k, the previous default), V0 (~245k), and fixed CBR 320k/256k/192k/128k. Opus offers 320k down to 96k.",
                    "Defaults are unchanged, so nothing gets quietly resampled on you. Lockable via MP3_BITRATE and OPUS_BITRATE env vars if you want to enforce a setting across all users.",
                ]
            },
            {
                heading: "Fixed: Apple Music Playlists Cut Off at ~300 Tracks",
                items: [
                    "Apple Music only server-renders around 300 tracks into the initial page HTML, so large playlists were getting silently chopped off.",
                    "MusicGrabber now fetches the public Apple Music page, extracts the current web bundle URL, pulls the web MusicKit bearer token from that bundle, and walks Apple's paginated `amp-api` track endpoint directly. If that API path fails, it still falls back to the old server-rendered HTML scrape instead of just giving up.",
                ]
            },
        ]
    },
    "2.6.0": {
        title: "What's New in v2.6.0",
        sections: [
            {
                heading: "Tag Editor Modal",
                items: [
                    "Queue items no longer open an inline tag form inside the constantly-refreshing queue card. 'Edit Tags' now opens a proper modal with artist, title, album, album artist, year, and track number fields, a filename preview, and reset/save actions. Your edits survive the queue refreshing underneath.",
                    "The tag editor also has a 'Guess from MusicBrainz' button that fills in album, album artist, year, and track numbering for completed downloads. If the first guess is close-but-wrong, 'Guess Again' walks through the next available candidate instead of giving you the same answer twice.",
                ]
            },
            {
                heading: "Search: Better Lossless Results",
                items: [
                    "All-source searches now give Monochrome/Tidal up to 10 scored candidates in the merged ranking (was a small fixed cap shared with all sources). Lossless results have more room to compete without every source flooding the list.",
                ]
            },
            {
                heading: "Fixed: Watched Playlist Mismatch Checker Too Strict",
                items: [
                    "Several patterns that should match were being incorrectly rejected. 'Paro House - Luciid VIP' (Spotify dash-form) now matches 'Paro House (Luciid VIP)' (Tidal bracket-form). 'NO DRAMA - Original Mix' matches 'NO DRAMA'. Single-word variant labels like 'TechnoBack' are now accepted as bracket equivalents.",
                    "The artist check for multi-artist credits (e.g. 'OGUZ, Nyctonian') now passes if any individual artist is mentioned in the result, rather than requiring the whole comma-joined string verbatim. Tidal typically credits only the primary artist, so the old check was silently failing good matches.",
                ]
            },
            {
                heading: "Fixed: Monochrome Duration Mismatch Now Falls Back",
                items: [
                    "When Tidal serves a shorter radio edit or different version that fails the MusicBrainz duration check, MusicGrabber now falls back to YouTube or mp3phoenix to find the right version, rather than just failing the download outright.",
                ]
            },
            {
                heading: "Fixed: Various Trash Bin Causes",
                items: [
                    "Monochrome scoring was penalising correct VIP/remix results when the variant was written with dashes in the query but brackets in the Tidal title. The -110 penalty is now skipped when the bracketed content is already present in the search query.",
                    "AcoustID now requires an artist match to accept a metadata override. Previously, a title-only match (score 9) was enough, which let cover versions overwrite the correct artist field and trigger a mismatch failure.",
                    "Channel names with distributor prefixes like 'Premiere Eczko' are now stripped to just 'Eczko' when used as the artist fallback.",
                ]
            },
            {
                heading: "Fixed: Large Playlist Import Crashes",
                items: [
                    "The database connection pool was exhausted under heavy concurrent load, causing 'database is locked' crashes mid-import. The pool now blocks briefly on checkout instead of spawning unlimited competing connections. Pool size also bumped from 5 to 8.",
                ]
            },
        ]
    },
    "2.5.6": {
        title: "What's New in v2.5.6",
        sections: [
            {
                heading: "SoundCloud Playlists",
                items: [
                    "You can now paste a SoundCloud sets URL or likes page into the watched playlists form and it works like any other platform. Full append/mirror sync, M3U generation, and per-playlist source selection all included.",
                    "Importing a SoundCloud playlist automatically pre-selects SoundCloud as the download source, since the tracks are already right there.",
                ]
            },
        ]
    },
    "2.5.5": {
        title: "What's New in v2.5.5",
        sections: [
            {
                heading: "Fixed: Duplicate Detection Missing Auto-Routed Album Folders",
                items: [
                    "If a single had already been auto-filed into an album subfolder, duplicate detection could still miss it because it only checked the flat artist folders. That meant a second download could sneak through and create another copy in the wrong place.",
                    "MusicGrabber now scans one level deeper inside artist folders when checking for duplicates, so tracks already routed into `Artist/Album/` are found properly before anything gets downloaded again.",
                ]
            },
            {
                heading: "Fixed: Soulseek 'Add to Playlist' Was Incomplete",
                items: [
                    "Soulseek downloads were not receiving the playlist parameters from the API layer, so 'Add to playlist' did not behave like the other sources. Duplicate-skip jobs could not finish as 'added to playlist', and successful Soulseek downloads were not appended to the physical `.m3u` file.",
                    "That path now receives the same playlist context as YouTube, Monochrome, and the rest, so Soulseek tracks can be added to playlist folders and M3Us correctly.",
                ]
            },
            {
                heading: "Fixed: Per-User Playlist M3Us Using the Wrong Folder",
                items: [
                    "Physical playlist updates were resolving the playlists directory without `user_id`, which meant user-specific playlist subfolder settings could be ignored in multi-user setups.",
                    "All playlist M3U writes now resolve the correct per-user playlists directory consistently across every source.",
                ]
            },
        ]
    },
    "2.5.4": {
        title: "What's New in v2.5.4",
        sections: [
            {
                heading: "Fixed: Monochrome Playback Endpoint Change",
                items: [
                    "Monochrome changed how its website fetches playable tracks. Search and info still worked, but the old `/track/` endpoint MusicGrabber used for downloads started returning `403 Upstream API error` for lots of perfectly normal tracks.",
                    "MusicGrabber now uses Monochrome's newer `/trackManifests/` playback endpoint with the same signed DASH manifest flow the website uses. It also rotates across multiple Monochrome instance hosts automatically, so one rate-limited instance no longer sinks the whole source.",
                ]
            },
            {
                heading: "Fixed: Monochrome Hover Preview",
                items: [
                    "The new Monochrome playback API returns a DASH manifest, which browsers will not play directly in a normal audio element. Hover preview now runs through a tiny server-side ffmpeg transcode to a short MP3 preview stream, so Monochrome previews work again.",
                ]
            },
            {
                heading: "Fixed: Watched Playlist Source Toggles Forgetting Their State",
                items: [
                    "The per-playlist source toggles (YT, PX, SC, MO) were saving correctly to the database, but the UI forgot your choices on every page load and cheerfully turned everything back on. Turns out the source list wasn't cached yet when the chips first rendered, so the re-population step saw an empty page and assumed you wanted all of them. Your preferences now survive page loads as intended.",
                ]
            },
            {
                heading: "Fixed: Watched Playlists Occasionally Grabbing the Wrong Song",
                items: [
                    "When searching for a watched playlist track, if no result mentioned the expected artist at all, the importer used to shrug and download the highest-scoring result anyway. This meant roughly 2% of a large playlist could end up as completely wrong tracks, which is a fun surprise if you like musical roulette, less so if you don't.",
                    "Now fails the track with a clear error explaining what it nearly downloaded, so you can retry it manually from the Missing panel instead of discovering the interloper six months later.",
                ]
            },
            {
                heading: "Fixed: Monochrome/Tidal CDN Wobbles",
                items: [
                    "Even after Monochrome hands back a valid playback manifest, the downstream Tidal CDN can still wobble with the occasional 403 or 429. The download path now retries with backoff before giving up, which makes transient edge failures much less fatal.",
                ]
            },
        ]
    },
    "2.5.3": {
        title: "What's New in v2.5.3",
        sections: [
            {
                heading: "New: Trash Bin",
                items: [
                    "Deleting a track now moves it to a trash folder instead of wiping it forever. Think of it as a recycling bin for your ears.",
                    "Changed your mind? Hit Restore in the Trash Bin to put it back exactly where it came from, no re-download needed.",
                    "The Queue tab now has a Trash Bin section at the bottom (only appears when there's something in it) with Restore and permanent Delete buttons per file, plus an 'Empty Trash' button for the brave.",
                    "Files that fail mismatch or duration checks now land in the trash too, so you can listen before the evidence disappears.",
                ]
            },
            {
                heading: "New: Play Button on Queue and Trash",
                items: [
                    "Completed downloads now have a play button in their expanded details. Quick way to check what actually got downloaded without leaving the tab.",
                    "Trashed files get a play button too, so you can listen before deciding whether to restore or permanently bin them. Especially useful for mismatch rejects that might be perfectly fine.",
                ]
            },
            {
                heading: "Fixed: Database Locking During Bulk Imports",
                items: [
                    "The new score rationale feature was accidentally opening a second database connection mid-transaction, which could cause 'database is locked' errors during bulk imports and watched playlist refreshes. Now shares the existing connection properly.",
                ]
            },
            {
                heading: "Fixed: ListenBrainz Weekly Playlists Stuck After Rotation",
                items: [
                    "If your ListenBrainz 'Created for You' playlists were added before the auto-rotation feature existed, they had no username stored and would get stuck showing 'playlist not found' every week after rotation. The refresh now self-heals by pulling the username out of the playlist name and updating the record, so it silently picks up the new week's playlist as intended.",
                ]
            },
        ]
    },
    "2.5.2": {
        title: "What's New in v2.5.2",
        sections: [
            {
                heading: "New: Download Progress Stages",
                items: [
                    "The queue now shows what each download is doing in real time instead of just sitting on 'downloading' until it finishes. You'll see stages like 'Fetching info', 'Downloading audio', 'Looking up metadata', 'Tagging file', 'Fetching lyrics', and more. Updates every 3 seconds.",
                    "Works across all sources: YouTube, SoundCloud, Monochrome/Tidal, MP3Phoenix, and Soulseek.",
                ]
            },
            {
                heading: "New: Concurrent Downloads Setting",
                items: [
                    "You can now control how many bulk import and watched playlist downloads run at the same time. Find it in Settings under General (admin-only), or set the MAX_CONCURRENT_DOWNLOADS env var. Range is 1-10, default 3. Higher is faster but increases the risk of bot detection.",
                ]
            },
            {
                heading: "New: \"Why This Result?\" Scoring Rationale",
                items: [
                    "When a bulk import or watched playlist download goes wrong, you can now find out exactly why the scorer picked that particular result. Failed and problematic queue items get a 'Why this result?' link in their expanded details.",
                    "Shows the winning candidate's score with human-readable reasons (e.g. 'Official channel +40', 'Live/session penalty -180'), plus the top 3 runners-up so you can see what it passed over and why.",
                    "There's a 'Raw scoring' expander for the technically curious, and a clipboard copy button if you want to share the evidence of the scorer's questionable life choices.",
                ]
            },
            {
                heading: "Fixed: Cross-Playlist Duplicate Mismatches",
                items: [
                    "If the same track appeared in two different watched playlists, the second playlist would mark it 'completed with errors' because Spotify and Monochrome/YouTube disagreed on punctuation (brackets vs hyphens, subtitles, etc.). The file was already on disk and perfectly fine, it was just the name check being overzealous. Duplicate-skip paths now bypass the mismatch comparison entirely.",
                ]
            },
            {
                heading: "Fixed: Watched Playlists Getting Too Into Live Versions",
                items: [
                    "Watched playlists are now much less likely to wander off with a live/session recording when what you actually wanted was the normal studio track. Performance-style results get hit with much heavier score penalties unless the query explicitly asks for one.",
                    "The live detector also learned some new vocabulary. It's no longer just looking for the word 'live'; it now catches the usual suspects like Tiny Desk, KEXP, Mahogany, COLORS, Radio 1, From The Basement, sessions, and other \"this definitely happened in front of people\" uploads across YouTube, Monochrome, MP3Phoenix, SoundCloud, and Soulseek.",
                    "On top of that, the watched-track matcher stops shrugging and treating 'live' as harmless title fluff, so a concert version no longer gets waved through as if it were the plain studio release.",
                ]
            },
            {
                heading: "Fixed: Track Numbers on Singles",
                items: [
                    "Single track downloads were leaving the TRACKNUMBER tag empty, which upset Beets and other library managers during lookups. MusicBrainz already had the data, we just weren't writing it to the file. Now uses a priority chain: album context first, then existing file tags (Tidal FLACs already have this baked in), then MusicBrainz as a fallback. Existing tags are never overwritten.",
                ]
            },
            {
                heading: "Fixed: First User Login & Button Breakage",
                items: [
                    "Creating the first user account now automatically refreshes the browser and shows the login page. Previously you'd be left on an unresponsive UI until you manually hit F5.",
                    "User management buttons (Remove, Force reset) and album artist Select button were silently broken by a quoting bug. They work now.",
                ]
            },
        ]
    },
    "2.5.1": {
        title: "What's New in v2.5.1",
        sections: [
            {
                heading: "Force Download for Mismatched Tracks",
                items: [
                    "Watched playlist tracks that fail the name-matching check now have a \"Force Download\" button in the Stats tab mismatch log and on the queue card itself. If you can see the track is correct but the names just don't line up (YouTube vs Spotify naming quirks, non-English characters, etc.), hit the button and it'll re-download without the comparison. The mismatch record is cleaned up automatically.",
                    "Works even if the original job has been cleaned up (e.g. via Stats reset). A fresh job is created from the expected artist/title and queued with the name check skipped.",
                    "Static assets (CSS, JS) are now cache-busted with the version number, so you'll always see new features immediately after an update without needing to hard-refresh.",
                ]
            },
            {
                heading: "Bug Fixes",
                items: [
                    "Opus files are now properly converted to your chosen audio format (MP3, FLAC, etc.) even when yt-dlp's built-in converter fails mid-stream. Previously, a failed conversion would leave the raw Opus file in your library.",
                    "MP3Phoenix downloads now respect your audio format setting instead of always converting to FLAC.",
                ]
            },
        ]
    },
    "2.5.0": {
        title: "What's New in v2.5.0",
        sections: [
            {
                heading: "New: Spotify Liked Songs",
                items: [
                    "Paste your Liked Songs URL (open.spotify.com/collection/tracks) into the playlist import or watched playlists field and it works like any other playlist. Requires the sp_dc cookie to be set in Settings, since Spotify considers your likes a private affair.",
                    "Works as a watched playlist too, so new likes get picked up automatically on the next refresh. If your cookies expire, you'll get a notification and a clear error on the playlist card rather than silent failures.",
                ]
            },
            {
                heading: "New: Proper Album Cover Art",
                items: [
                    "Every download now tries really hard to find proper album artwork instead of relying on YouTube video thumbnails. The fallback chain: Cover Art Archive (MusicBrainz), Tidal CDN (Monochrome tracks), iTunes Search API, then Deezer API. No API keys needed, no configuration required.",
                    "Soulseek and MP3Phoenix downloads, which previously had zero cover art, now get artwork from the same fallback chain. If all sources come up empty, the yt-dlp thumbnail is kept as a last resort.",
                    "Monochrome/Tidal cover art now survives format conversion to MP3/Opus. Previously it was embedded into the FLAC but lost when converting to other formats.",
                ]
            },
            {
                heading: "New: Search to Album Shortcut",
                items: [
                    "Search for 'Artist - Title' and MusicGrabber will look up the artist's discography on MusicBrainz in the background. If the track belongs to a known album, an 'Artist and Album' chip appears in the Related Searches box. One click takes you straight to the Albums tab with the full tracklist loaded, ready for download.",
                    "Monochrome/Tidal results also get a clickable album name in the result line. Spot a track from a good album? Click the album name and you're there.",
                ]
            },
            {
                heading: "New: Configurable Download Timeouts",
                items: [
                    "If you've ever had a long track (DJ mixes, live recordings, symphonies) come out broken or truncated, the download or conversion was probably hitting a hard time limit. Those limits are now configurable via environment variables in your docker-compose: TIMEOUT_YTDLP_DOWNLOAD, TIMEOUT_FFMPEG_CONVERT, and TIMEOUT_MP3PHOENIX_DOWNLOAD. Defaults are unchanged, so nothing breaks if you don't touch them.",
                ]
            },
            {
                heading: "New: Paginated Queue",
                items: [
                    "The download queue now shows the last 250 jobs, paginated 10 at a time with prev/next controls. No more scrolling through your entire download history to find that one track that failed. The 'Downloadable to Device' list is similarly paginated at 15 per page.",
                ]
            },
            {
                heading: "Improvements",
                items: [
                    "Spotify music video rows are now parsed correctly. Previously, tracks marked as music videos could be silently dropped if the artist couldn't be extracted from the title. The scraper now finds the artist via Spotify's bullet separator, which handles explicit markers and other row variations without falling over.",
                    "Artist search in Albums and Watched tabs now respects case sensitivity. Searching for 'SiR' no longer returns every artist named 'Sir'. Exact case match floats to the top.",
                    "File permissions now apply to artist and album directories too, not just the files inside them. If your NAS could see the files but the folder itself had the wrong permissions, that's sorted.",
                ]
            },
            {
                heading: "Security Fixes",
                items: [
                    "Session tokens and download tokens now expire when they're supposed to. A timestamp format mismatch meant tokens could stay valid a bit longer than intended on their expiry day. Sorted.",
                    "In multi-user mode, regular users could previously change their music directory to any path on the server. That's now admin-only, as it should be.",
                    "The 'Test Connection' buttons for Navidrome, Jellyfin, and Lidarr could be used by non-admin users to poke arbitrary URLs from the server. Non-admins can still test their own saved settings, but can no longer supply custom URLs.",
                    "SQLite foreign key cascades (cleanup of sessions, tokens, and settings when a user is deleted) were declared in the database schema but never actually switched on. They work now, so deleting a user properly cleans up after itself.",
                ]
            }
        ]
    },
    "2.4.6": {
        title: "What's New in v2.4.6",
        sections: [
            {
                heading: "New: configurable file permissions",
                items: [
                    "Downloaded files have always been set to 666 (rw for everyone) for NAS and SMB compatibility. If your media server or NAS refuses to write to files owned by root, you can now switch to 777 in Settings (admin only). Two options, no free-text field; we're not animals.",
                ]
            },
            {
                heading: "New: sub-hourly watched playlist intervals",
                items: [
                    "Refresh intervals now go down to every 30 minutes, with hourly, 6h, and 12h options added alongside the existing daily/weekly/monthly. The backend already supported it; the UI just hadn't bothered to expose it. Bot-ban consequences are your own.",
                ]
            },
            {
                heading: "Bug fixes",
                items: [
                    "The 'Skip duplicates' toggle was gaslighting you; it looked saved, then quietly forgot every time you refreshed. The setting is now actually written to the database like it promised.",
                    "The playlist routing selector was showing a 'will be overwritten on sync' warning for Append playlists, which don't get overwritten on sync at all. The warning now only appears for Mirror playlists, where it actually means something.",
                    "Tracks stuck in Missing because their download job failed, despite the file already being on disk from another route (manual download, different playlist), will now be spotted and cleared on the next refresh.",
                ]
            }
        ]
    },
    "2.4.5": {
        title: "What's New in v2.4.5",
        sections: [
            {
                heading: "Bug fixes",
                items: [
                    "If your music folder wasn't actually /music, Settings was telling porkies. It now shows the real path.",
                    "MusicGrabber can now live under a reverse-proxy subpath like /musicgrabber without losing all its CSS, JS, and API calls in the woods. Set ROOT_PATH and let your proxy strip it.",
                    "A bunch of Watched tab buttons had stage fright and stopped responding. Search, Retry, Tracks, Copy URL, Delete, and friends are awake again, and manual Search now jumps back to Results with the right playlist already picked.",
                    "Duplicate tracks that already exist in one playlist folder are now spotted for the next playlist too. No more marking a perfectly good track as failed just because it already lives somewhere on disk.",
                ]
            }
        ]
    },
    "2.4.4": {
        title: "What's New in v2.4.4",
        sections: [
            {
                heading: "Bug fixes",
                items: [
                    "Non-admin users were getting a 403 when hitting any 'Test connection' button in Settings, for Navidrome, Jellyfin, Lidarr, Apprise, and both cookie fields. Settings saved fine, but the test buttons were gated admin-only by mistake. Fixed.",
                ]
            }
        ]
    },
    "2.4.3": {
        title: "What's New in v2.4.3",
        sections: [
            {
                heading: "Auto-album routing for singles",
                items: [
                    "New opt-in setting: when enabled, singles with a MusicBrainz album match are automatically moved into Artist/Album/ after download, complete with track number tags. Falls back silently to Singles/Artist/ for new or unrecognised tracks; no errors, no fuss.",
                    "A second toggle, 'Route to Albums folder', sends matched tracks to Albums/Artist/Album/ instead of Singles/Artist/Album/. Handy if you want a clean Artist/Album/Track layout without touching your Singles folder.",
                    "Find both settings in Settings under Library. Both are off by default.",
                ]
            },
            {
                heading: "Bug fixes",
                items: [
                    "Monochrome downloads weren't triggering album routing at all. Tidal gives us the album title directly, so the routing now uses that instead of waiting for MusicBrainz. Fixed.",
                    "MusicBrainz was routinely picking radio compilations and promo discs as the canonical album: 'Promo Only Modern Rock Radio, December 2001' instead of the actual studio album. The release picker now scores options and strongly prefers studio albums by the actual artist, penalising Various Artists credits, compilations, and anything with 'Promo Only', 'Greatest Hits', or 'Best Of' in the title.",
                    "Re-download was picking the same bad result every time. It now excludes the failed video ID and searches for a fresh candidate.",
                    "MusicBrainz album and track number data was being skipped for most tracks; the lookup was only triggered when a year was missing, which is almost never. Fixed.",
                    "MP3Phoenix downloads were storing the artist as Unknown in the queue. The artist from the search result is now passed through correctly.",
                    "The auto-album routing toggle wasn't saving due to a missing field in the settings model. Fixed.",
                ]
            }
        ]
    },
    "2.4.2": {
        title: "What's New in v2.4.2",
        sections: [
            {
                heading: "Bug fixes",
                items: [
                    "Fresh installs were broken; the database schema was missing columns added in recent releases, causing immediate errors on first run. Upgraders were fine as their databases were patched automatically, but anyone starting fresh from v2.4.0 or v2.4.1 would hit a crash. Fixed.",
                    "Mid-track silence detection: ffmpeg now scans the first 60% of every downloaded track for suspicious gaps of 8+ seconds. This catches Content ID fraud uploads where someone pads a track with silence in the middle to avoid fingerprinting while still matching the expected duration. The first 15 seconds and the final 40% are ignored, so legitimate long intros and hidden tracks on album closers are left alone.",
                    "WebM remux safety: album-routed files that need remuxing are now verified before the original is deleted. A corrupt output no longer silently destroys the source.",
                    "MBID validation: invalid MusicBrainz IDs now fail fast with a clear error rather than quietly failing deep in a lookup.",
                    "MP3Phoenix downloads: size is checked after download; truncated files are deleted immediately rather than left as stubs.",
                ]
            }
        ]
    },
    "2.4.1": {
        title: "What's New in v2.4.1",
        sections: [
            {
                heading: "Bug fix",
                items: [
                    "ListenBrainz 'Created for You' playlists (Weekly Exploration, Weekly Jams, etc.) rotate to a new URL every Monday. MusicGrabber was storing the old UUID and refreshing a dead playlist forever. It now re-resolves the current week's URL automatically going forward.",
                    "Action required for existing users: delete your ListenBrainz watched playlist cards and re-add them using your ListenBrainz username. This lets MusicGrabber store the information it needs to auto-update the URL each week. Cards added before this update won't self-heal.",
                ]
            }
        ]
    },
    "2.4.0": {
        title: "What's New in v2.4.0",
        sections: [
            {
                heading: "Album Download tab",
                items: [
                    "There's now a dedicated Albums tab for downloading full albums intentionally. Search for an artist, pick an album, preview the tracklist, and download the lot in one go.",
                    "Files are saved as Albums/Artist/Album/Track rather than getting mixed in with Singles. No more manually sorting things after the fact.",
                    "Optionally generate an M3U playlist alongside the download.",
                    "Artist and album data comes from MusicBrainz, so you get proper metadata rather than YouTube's creative guesswork.",
                    "The Albums folder path is configurable in Settings, right next to the Singles and Playlists folders.",
                    "Only missing tracks are queued; if half the album is already there, only the gaps are downloaded. The precheck tells you upfront how many tracks exist and how many will be fetched.",
                ]
            },
            {
                heading: "Unified \"Add to...\" destination picker",
                items: [
                    "The separate \"Add to playlist\" and \"Add to album\" chips in search results have been replaced by a single \"Add to...\" button.",
                    "Choosing Album opens a two-level browser: pick an artist folder, then an album folder. MusicGrabber reads the .albuminfo sidecar written at download time to get the MusicBrainz context automatically.",
                    "If auto-matching can't place the track, a manual track picker appears so you can select the right slot yourself.",
                    "Folders without an .albuminfo sidecar still work; the track lands in the right folder, just without MusicBrainz metadata enrichment.",
                ]
            },
            {
                heading: "Album quality-of-life fixes",
                items: [
                    "TRACKTOTAL tags are now correct when only some tracks were missing. Previously a partial download (e.g. 3 of 12 tracks) would tag those files 3/3 instead of 3/12.",
                    "Album M3U files now include proper duration and title entries so players show correct metadata immediately, without waiting for a library scan.",
                    "Clicking Download Album twice in quick succession no longer queues everything twice and races to download the same files. The second click is a no-op if an import is already running for that album.",
                ]
            },
            {
                heading: "Watched Artists",
                items: [
                    "Follow an artist by MusicBrainz ID and new singles are downloaded automatically as they appear. Same controls as watched playlists: check interval, convert-to-FLAC, missing panel, track list.",
                    "Singles only; remixes, live versions, soundtracks, DJ mixes, and compilations are filtered out at the MusicBrainz level.",
                    "Tracks already on disk are recognised on first refresh, so you won't re-download things you already have.",
                ]
            },
            {
                heading: "Watched playlist matching improvements",
                items: [
                    "Scandinavian and other non-decomposable characters (Ø, ø, Ł, æ, ß, etc.) now normalise correctly. BYØRN was failing to match BYORN because NFKD can't decompose those letters.",
                    "Tracks namespaced under a mixtape or project in ALL CAPS (e.g. STONEHENGE - GEEKED UP) now strip the leading prefix before matching, so they resolve to the correct track.",
                ]
            },
            {
                heading: "Bug fixes",
                items: [
                    "Monochrome search results no longer show duplicate cards for the same recording at different quality tiers. The highest quality entry is kept and given a scoring boost; the duplicates are dropped.",
                    "Monochrome downloads that fail on all quality tiers now search across all enabled sources for the best alternative, rather than defaulting straight to YouTube.",
                    "Fixed a crash on /api/playlists when the Playlists directory was on a broken or stale mount. Returns an empty list now instead of a 500 error.",
                ]
            }
        ]
    },
    "2.3.5": {
        title: "What's New in v2.3.5",
        sections: [
            {
                heading: "Bug fixes",
                items: [
                    "Manual downloads no longer get rejected by the MusicBrainz duration check. If you picked a specific track from the search results, MusicGrabber now trusts your judgement. Automated downloads (watched playlists, bulk import) still reject duration mismatches.",
                    "The \"Add to playlist\" dropdown now refreshes every time you open it, rather than only on page load. Fixes cases where the initial load failed silently and left the list empty.",
                ]
            }
        ]
    },
    "2.3.4": {
        title: "What's New in v2.3.4",
        sections: [
            {
                heading: "Apple Music import",
                items: [
                    "Public Apple Music playlists and albums can now be imported and watched. Paste the URL in the Watch or bulk import box.",
                    "No browser or API key needed; Apple server-renders the full track list, so a plain HTTP fetch is all it takes.",
                    "Supports all regional storefronts. Private playlists and personal libraries aren't accessible (Apple won't let us in without a sign-in).",
                ]
            },
            {
                heading: "ALAC output format",
                items: [
                    "ALAC (Apple Lossless) is now a selectable audio format in Settings, alongside FLAC, Opus, and MP3.",
                    "Files are saved as .m4a. Lossless quality, great for modded iPods and Apple devices that don't speak FLAC.",
                ]
            },
            {
                heading: "Bug fixes",
                items: [
                    "Watched tracks in album-structured folders (e.g. from Monochrome) were being re-queued as missing on every refresh. MusicGrabber now checks the exact path it recorded at download time, so tracks outside the Singles folder are recognised correctly.",
                    "The download queue now shows \"Already in library\" instead of a raw file path for duplicate-skipped tracks. The full path is still there if you expand the job.",
                ]
            }
        ]
    },
    "2.3.3": {
        title: "What's New in v2.3.3",
        sections: [
            {
                heading: "Per-playlist source selection",
                items: [
                    "Each watched playlist now has a Sources row with toggleable chips, one per search source (YouTube, SoundCloud, MP3Phoenix, Monochrome).",
                    "By default all sources are active. Deselect any you don't want used for that playlist.",
                    "The Watch form also has the selector so you can set preferences on the way in.",
                    "If you pick a source that's globally disabled in Settings, MusicGrabber quietly falls back to all enabled sources instead of finding nothing.",
                    "Bug fix on playlist selector fixed.",
                ]
            }
        ]
    },
    "2.3.2": {
        title: "What's New in v2.3.2",
        sections: [
            {
                heading: "Lidarr duplicate check",
                items: [
                    "If you run Lidarr alongside MusicGrabber, you can now point MusicGrabber at your Lidarr instance (Settings → Lidarr, URL and API key). Before downloading anything, MusicGrabber will check whether the track already exists in your Lidarr library. If it does, the download is skipped",
                    "Real file paths are resolved via Lidarr's trackfile API, so watched playlist M3U entries include the correct path to the Lidarr-managed file - useful if you use Plex, Plexamp, or any other player that reads M3Us from a shared music directory",
                    "Runs as a final fallback after the local filesystem and Navidrome checks, so it doesn't slow things down when the track is already in your MusicGrabber library"
                ]
            },
            {
                heading: "Bulk import crash fix",
                items: [
                    "Bulk import was broken - pasting tracks into the box would appear to start but immediately fail silently. The background worker was being called with a user_id argument it doesn't accept; it reads that from the database itself. One redundant argument removed, bulk import works again"
                ]
            },
            {
                heading: "Watched playlist track order",
                items: [
                    "M3U files generated from watched playlists now follow the same track order as the source playlist. Previously they were ordered by when MusicGrabber first saw each track, which was effectively random for the initial sync",
                    "Positions are updated on every refresh, so if someone reorders the source playlist MusicGrabber will reflect that on the next check",
                    "Existing playlists get correct ordering automatically after their next refresh - no manual intervention needed"
                ]
            }
        ]
    },
    "2.3.1": {
        title: "What's New in v2.3.1",
        sections: [
            {
                heading: "Spotify music video fix",
                items: [
                    "Playlists containing music video entries no longer break the import. Spotify labels these with 'Music Video' as the artist, so both the embed scraper and the headless browser now detect this and attempt to salvage the real artist and title from the track name. If it can't be parsed cleanly, the entry is skipped rather than imported as garbage",
                    "Tested against a 2000+ track playlist with a mix of music videos and regular tracks - all came through correctly"
                ]
            },
            {
                heading: "Single-user mode restored after deleting accounts",
                items: [
                    "If you deleted all guest accounts and only your own admin account remained, MusicGrabber would incorrectly keep showing the login screen. Multi-user mode now only activates when there are two or more accounts - one account is treated the same as none"
                ]
            },
            {
                heading: "UI polish",
                items: [
                    "Preview audio fades in over 5 seconds rather than jumping to full volume",
                    "The Save Settings button, Ko-fi link, and Release Notes button now live in a fixed bar at the bottom of the settings page, always within reach no matter how far you've scrolled",
                    "Create a new playlist on the fly from the search results page - no need to go to the Watched tab first"
                ]
            },
            {
                heading: "Security",
                items: [
                    "Several admin-only endpoints (stats, job cleanup, check-all, blacklist) were missing access checks and were reachable by regular users. Fixed",
                    "Search token validation is now scoped to the user who issued the token"
                ]
            }
        ]
    },
    "2.3.0": {
        title: "What's New in v2.3.0 - g33kphr33k's Birthday Edition",
        sections: [
            {
                heading: "Important - database migration",
                warning: "This update modifies the database schema. MusicGrabber will run the migration automatically on first start and it is safe to run on an existing install - no data is lost. That said, take a backup of your /data/music_grabber.db before upgrading, just in case. Downgrading to v2.2.x after running the migration is not supported."
            },
            {
                heading: "Multi-user support",
                items: [
                    "Multiple people can now share a MusicGrabber instance without stepping on each other. Each user has their own download history, watched playlists, watched artists, and bulk imports",
                    "Per-user settings: each user configures their own music directory, Navidrome and Jellyfin credentials, and notification endpoints independently",
                    "Out of the box, everything works exactly as before - no login required until you create an account. Multi-user mode kicks in the moment you create your first user in Settings",
                    "Login is username and password, sessions last 30 days, and the old API key still works for scripts and integrations",
                    "Admin role manages global settings (audio format, slskd, YouTube cookies, Spotify settings) and can create, remove, and reset user accounts",
                    "YouTube cookies are now per-user, so one user's authenticated downloads don't collide with another's"
                ]
            },
            {
                heading: "Spotify private playlists",
                items: [
                    "Paste your Netscape-format cookies.txt from open.spotify.com into Settings to unlock private playlists, saved albums, and personal library playlists - anything that requires a Spotify login",
                    "Works per-user, same pattern as YouTube cookies. If the cookies expire mid-use, an amber banner appears in Settings and a clear message is shown when you try to fetch a playlist",
                    "See the README for step-by-step instructions on exporting cookies from your browser"
                ]
            },
            {
                heading: "Search source toggles",
                items: [
                    "Each search source (YouTube, MP3Phoenix, SoundCloud, Monochrome) can now be individually enabled or disabled in Settings",
                    "Applies everywhere: search results, watched playlist matching, and bulk imports all skip disabled sources",
                    "Useful if a source is slow, unreliable, or just not relevant to what you're grabbing",
                    "Bug fix included: the toggle was silently broken on first release - disabled sources were still appearing in results. Now fixed"
                ]
            },
            {
                heading: "Search quality improvements",
                items: [
                    "Monochrome lossless results now reliably beat 320 kbps MP3 results on equal relevance. The scoring gap between lossless and lossy was too narrow and could be flipped by a small duration scoring nudge",
                    "A single source can no longer flood the results. Previously, MP3Phoenix could contribute ten near-identical tracks and push a Monochrome lossless result off the page entirely. Each source is now capped at four results in the merged pool before quality scoring decides the final order",
                    "Tidal variant tracks (e.g. 'Hey Man Nice Shot (½ oz)') no longer float to the top just because they're lossless. Unknown parenthetical suffixes are now treated as a variant signal and penalised accordingly. Standard suffixes like Remastered and Deluxe Edition are unaffected",
                    "MusicBrainz duration matching is tighter. The old tolerances were calibrated for long tracks; most songs are around 3 minutes, where the previous 25% band was 45 seconds of slop. Bands are now much stricter"
                ]
            },
            {
                heading: "Security improvements",
                items: [
                    "Login brute-force protection: accounts lock after repeated failed attempts",
                    "File download links are now short-lived single-use tokens - the session token no longer appears in any URL",
                    "HTTPS-only mode available via HTTPS_ONLY=true environment variable",
                    "API key in query params is now opt-in (ALLOW_API_KEY_QUERY_PARAM=true) to prevent credentials leaking into proxy logs",
                    "XSS audit complete - all API data rendered into the page now goes through escapeHtml()"
                ]
            },
            {
                heading: "Bug fixes",
                items: [
                    "Monochrome tracks now respect the audio format setting - if you asked for MP3 or Opus, you'll actually get it instead of quietly keeping FLAC",
                    "Docker healthcheck now respects the LISTEN_PORT environment variable instead of always probing port 8080"
                ]
            }
        ]
    },
    "2.2.7": {
        title: "What's New in v2.2.7",
        sections: [
            {
                heading: "What is Music Grabber?",
                body: "Music Grabber is a self-hosted tool for grabbing individual tracks. You heard something on the radio, in a film, at a party - you want that song. It searches YouTube, SoundCloud, and Monochrome (Tidal lossless) in parallel, downloads the best quality version, and drops it neatly into your library. It is not Lidarr. It does not manage your collection. It just gets the track. This is a personal pet project, actively developed but rough around the edges. If something breaks, please check the issue tracker before raising a duplicate."
            },
            {
                heading: "Bug fixes in this version",
                items: [
                    "Watched track matching no longer falls over when YouTube uses fullwidth Unicode punctuation (｜ instead of |, － instead of -) in video titles. Titles that looked identical in the logs but weren't are now correctly matched",
                    "ListenBrainz 'Created for You' playlists now actually populate with tracks when added as a watched playlist. The listing API returns empty track arrays - each playlist has to be fetched individually to get its contents, which we weren't doing"
                ]
            }
        ]
    },
    "2.2.6": {
        title: "What's New in v2.2.6",
        sections: [
            {
                heading: "What is Music Grabber?",
                body: "Music Grabber is a self-hosted tool for grabbing individual tracks. You heard something on the radio, in a film, at a party - you want that song. It searches YouTube, SoundCloud, and Monochrome (Tidal lossless) in parallel, downloads the best quality version, and drops it neatly into your library. It is not Lidarr. It does not manage your collection. It just gets the track. This is a personal pet project, actively developed but rough around the edges. If something breaks, please check the issue tracker before raising a duplicate."
            },
            {
                heading: "Watched Artists - new feature",
                items: [
                    "Follow an artist on MusicBrainz and new singles are downloaded automatically as they appear. Search by name, pick from up to five candidates, set a from-date (defaults to today so your back-catalogue stays put), and MusicGrabber does the rest",
                    "Singles only - remixes, live versions, soundtracks, DJ mixes, and compilations are filtered out at the MusicBrainz level so you don't get flooded with every variant ever released",
                    "Tracks already on disk are recognised immediately on first refresh - no duplicate downloads",
                    "Same controls as watched playlists: check interval, convert-to-FLAC, pause/resume, missing panel, track list with download buttons"
                ]
            },
            {
                heading: "Other new bits",
                items: [
                    "Downloadable to Device: new section at the bottom of the Queue tab lists every completed download newest-first with a Save button to pull it straight to your browser - handy for grabbing tracks to a phone or laptop",
                    "Save to device buttons on watched playlist and artist track lists",
                    "Adding a watched playlist now shows a spinner and elapsed timer instead of sitting silently with a greyed-out button",
                    "YouTube cookies no longer cause 'Requested format is not available' - the format selector now skips Premium-only streams that your account can't actually download"
                ]
            },
            {
                heading: "Bug fixes",
                items: [
                    "Watched track matching is smarter: Spotify dash-suffixes, bracketed equivalents, and pipe-separated session tags (Tugboats | OurVinyl Sessions) all resolve to the same track instead of triggering a mismatch delete and re-download",
                    "Tracks in playlist folders are no longer falsely marked as deleted by the file reconciler",
                    "Format errors no longer trigger the bot-block backoff sleep - that sleep is for genuine 403s, not manifest mismatches"
                ]
            }
        ]
    },
    "2.2.5": {
        title: "What's New in v2.2.5",
        sections: [
            {
                heading: "What is Music Grabber?",
                body: "Music Grabber is a self-hosted tool for grabbing individual tracks. You heard something on the radio, in a film, at a party - you want that song. It searches YouTube, SoundCloud, and Monochrome (Tidal lossless) in parallel, downloads the best quality version, and drops it neatly into your library. It is not Lidarr. It does not manage your collection. It just gets the track. This is a personal pet project, actively developed but rough around the edges. If something breaks, please check the issue tracker before raising a duplicate."
            },
            {
                heading: "Short and sweet",
                items: [
                    "MP3Phoenix is now fully wired in, with much more reliable queueing and fallback behavior",
                    "Watched playlist refresh status is clearer and the timer now starts fresh when you click Refresh",
                    "Manual file deletes/renames now reconcile automatically in the background so queue and watched states stay in sync"
                ]
            }
        ]
    },
    "2.2.4": {
        title: "What's New in v2.2.4",
        sections: [
            {
                heading: "What is Music Grabber?",
                body: "Music Grabber is a self-hosted tool for grabbing individual tracks. You heard something on the radio, in a film, at a party - you want that song. It searches YouTube, SoundCloud, and Monochrome (Tidal lossless) in parallel, downloads the best quality version, and drops it neatly into your library. It is not Lidarr. It does not manage your collection. It just gets the track. This is a personal pet project, actively developed but rough around the edges. If something breaks, please check the issue tracker before raising a duplicate."
            },
            {
                heading: "Bug fixes in this version",
                items: [
                    "Watched playlist cards now warn you when Navidrome has stale entries pointing to files that no longer exist on disk. If the M3U looks shorter than expected, an amber warning banner on the card will tell you exactly how many dead entries were found and what to do about it (Navidrome > Settings > Missing Files > Remove from Database)"
                ]
            }
        ]
    },
    "2.2.3": {
        title: "What's New in v2.2.3",
        sections: [
            {
                heading: "What is Music Grabber?",
                body: "Music Grabber is a self-hosted tool for grabbing individual tracks. You heard something on the radio, in a film, at a party - you want that song. It searches YouTube, SoundCloud, and Monochrome (Tidal lossless) in parallel, downloads the best quality version, and drops it neatly into your library. It is not Lidarr. It does not manage your collection. It just gets the track. This is a personal pet project, actively developed but rough around the edges. If something breaks, please check the issue tracker before raising a duplicate."
            },
            {
                heading: "Bug fixes in this version",
                items: [
                    "Watched playlist imports no longer get confused by remix and collaborator formatting differences between Spotify and YouTube. Tracks like 'Only Human - MPH Remix' on Spotify now correctly match 'Only Human (MPH Remix)' from YouTube, and artist comparisons handle order swaps, remixer additions, and separator differences (comma vs feat. vs x vs &)",
                    "MusicBrainz now checks the expected track duration after download. If the file is more than 10% shorter or longer than MusicBrainz expects, it's rejected as the wrong version. Catches wrong edits that pass all other integrity checks",
                    "Karaoke, nightcore, sped-up, slowed, 8D audio, and bass-boosted versions are now scored so low they cannot win a search result regardless of other factors. Nobody asked for the karaoke version",
                    "Bootleg edits, flips, refixes, reworks, and mashups are penalised heavily unless the search query specifically names them",
                    "Copyright-filtered uploads (pitch-shifted or muted to dodge Content ID) are disqualified. The audio is useless anyway",
                    "YouTube titles with leading label prefixes like '[UKF release] S.P.Y - Sweet Sound' now correctly parse to S.P.Y as the artist, instead of absorbing the prefix into the artist name",
                    "Watched playlist refresh no longer re-queues tracks that already exist in Navidrome but not in MusicGrabber's own folders",
                    "Audio integrity check now also catches preview segments baked with a large start_time offset - these sounded fine in ffprobe but played from the middle of the song"
                ]
            }
        ]
    },
    "2.2.2": {
        title: "What's New in v2.2.2",
        sections: [
            {
                heading: "What is Music Grabber?",
                body: "Music Grabber is a self-hosted tool for grabbing individual tracks. You heard something on the radio, in a film, at a party - you want that song. It searches YouTube, SoundCloud, and Monochrome (Tidal lossless) in parallel, downloads the best quality version, and drops it neatly into your library. It is not Lidarr. It does not manage your collection. It just gets the track. This is a personal pet project, actively developed but rough around the edges. If something breaks, please check the issue tracker before raising a duplicate."
            },
            {
                heading: "Bug fixes in this version",
                items: [
                    "Monochrome downloads now support the new DASH MPD manifest format, in addition to the old JSON format. HI_RES_LOSSLESS was silently failing for many tracks because the response format changed; it now works again",
                    "Monochrome quality ladder now tries HI_RES_LOSSLESS, then LOSSLESS, then HIGH, then LOW before falling back to YouTube, so you reliably get the best available tier",
                    "Downloads are now validated with ffprobe after completing. Truncated or corrupt files are retried, and if they keep failing the candidate is blacklisted so you don't get the same bad file twice",
                    "Soulseek results now factor in actual search relevance, not just format quality. A FLAC of the wrong song no longer beats an MP3 of the right one",
                    "Watched playlist refresh now checks whether local files actually exist before trusting the database. Manually deleted tracks are automatically re-queued on the next refresh",
                    "Watched playlists no longer mark the wrong track as downloaded. If the downloaded metadata doesn't match what was expected, the job is flagged as completed with errors instead of silently logged as done",
                    "Navidrome Test Connection now auto-enables real file paths for MusicGrabber players. Without this, M3U playlist entries could contain synthetic Navidrome paths instead of real ones",
                    "Stats tab no longer crashes when the Playlists folder is disabled",
                    "Service bind address and port are now configurable via LISTEN_ADDR and LISTEN_PORT environment variables, for IPv6 or non-standard setups"
                ]
            }
        ]
    },
    "2.2.1": {
        title: "What's New in v2.2.1",
        sections: [
            {
                heading: "What is Music Grabber?",
                body: "Music Grabber is a self-hosted tool for grabbing individual tracks. You heard something on the radio, in a film, at a party - you want that song. It searches YouTube, SoundCloud, and Monochrome (Tidal lossless) in parallel, downloads the best quality version, and drops it neatly into your library. It is not Lidarr. It does not manage your collection. It just gets the track. This is a personal pet project, actively developed but rough around the edges. If something breaks, please check the issue tracker before raising a duplicate."
            },
            {
                heading: "Bug fixes in this version",
                items: [
                    "Monochrome no longer downloads the wrong artist when a track isn't on Tidal - a lossless score bonus was overriding artist matching, so 'Venjent - Who Are Ya' could come back as 'Wolf Parade - Who Are Ya'. Fixed",
                    "Queue 'Already exists' messages now show Artist/filename instead of a bare track number like 01-01 - Title.flac",
                    "YouTube cookies causing 'Requested format is not available' now trigger the cookieless retry, same as 403 errors",
                    "Navidrome duplicate check now matches artists with curly apostrophes (Guns N\u2019 Roses vs Guns N\u0027 Roses) and handles compilation tracks where albumArtist is Various Artists"
                ]
            }
        ]
    },
    "2.2.0": {
        title: "What's New in v2.2.0",
        sections: [
            {
                heading: "What is Music Grabber?",
                body: "Music Grabber is a self-hosted tool for grabbing individual tracks. You heard something on the radio, in a film, at a party - you want that song. It searches YouTube, SoundCloud, and Monochrome (Tidal lossless) in parallel, downloads the best quality version, and drops it neatly into your library. It is not Lidarr. It does not manage your collection. It just gets the track. This is a personal pet project, actively developed but rough around the edges. If something breaks, please check the issue tracker before raising a duplicate."
            },
            {
                heading: "New in this version",
                items: [
                    "Watched playlists now show a full track list with per-track status and a Replace button for bad downloads",
                    "Playlist routing - route any download directly into a watched playlist or .m3u file from the search results",
                    "Monochrome 403 fallback now actually works (was broken - YouTube results were being crowded out by Monochrome's own score bonuses)",
                    "Missing track Retry and Search buttons on watched playlist cards",
                    "Apprise notification support - one URL covers Gotify, ntfy, Discord, Pushover, Slack, and about 50 others",
                    "Navidrome pre-download duplicate check - skips re-downloading tracks already in your library",
                    "ListenBrainz 'Created for You' playlist watching - Weekly Jams, Exploration Playlist, etc.",
                    "Frontend split into index.html + app.js - same behaviour, easier to navigate"
                ]
            }
        ]
    }
};
