// =============================================================================
// Search progress quips
// =============================================================================
// Cheeky-but-honest status lines for the live search progress panel.
// One global object, mirroring how release-notes.js is loaded before app.js.
//
// Structure:
//   SEARCH_QUIPS.searching[<source>]  -> shown while a source is still being searched
//   SEARCH_QUIPS.searching._default   -> fallback for any unknown source
//   SEARCH_QUIPS.empty                -> source answered but found nothing
//   SEARCH_QUIPS.timeout              -> source took too long and got left behind
//   SEARCH_QUIPS.offline              -> source is parked/offline and was skipped
//   SEARCH_QUIPS.error                -> source threw a wobbly
//
// House style: British, light, factual-ish, and absolutely no emdashes.
// Add as many as you like; one is picked at random.

const SEARCH_QUIPS = {

    searching: {

        youtube: [
            "Poking YouTube with a stick",
            "Sifting the YouTube haystack",
            "Wading through the reaction videos",
            "Skipping past the ten-hour loops",
            "Ignoring 4,000 lyric videos",
            "Asking YouTube to be serious for one second",
            "Scrolling past the sped-up nightcore",
            "Filtering out the bedroom covers",
            "Dodging the 'official' bootlegs",
            "Squinting at the thumbnails",
            "Politely declining the algorithm's suggestions",
            "Hunting the actual song, not the meme",
            "Wrestling the recommendation engine",
            "Looking past the 'FULL ALBUM (no ads)' uploads",
            "Looking past fourteen copies labelled BEST QUALITY",
            "Checking whether 'official audio' means either word",
            "Avoiding thumbnails with unnecessary red arrows",
            "Wading through videos filmed from three streets away",
            "Skipping the version pitched up for legal reasons",
            "Searching beneath several tonnes of algorithm",
            "Looking for the song between two sponsored opinions",
        ],

        monochrome: [
            "Rummaging through Qobuz's bins",
            "Leaning on the lossless lot",
            "Asking Deezer ever so nicely",
            "Checking the hi-res shelf",
            "Cross-examining the ISRCs",
            "Negotiating with the proxies",
            "Demanding nothing less than FLAC",
            "Holding out for the studio master",
            "Reading the fine print on the bitrate",
            "Verifying it actually exists before bragging",
            "Tutting at anything under 16-bit",
            "Politely refusing the live version",
            "Triangulating Tidal and Qobuz",
            "Insisting on the proper master",
            "Polishing the bits until they look lossless",
            "Checking whether 'hi-res' is wearing a fake moustache",
            "Inspecting the waveform with unnecessary suspicion",
            "Asking Qobuz where it keeps the good crockery",
            "Rejecting MP3s attempting to pass as respectable",
            "Counting bits like a particularly dull accountant",
        ],

        soundcloud: [
            "Leafing through SoundCloud",
            "Nudging the SoundClouders",
            "Wading past the DJ mixes",
            "Skipping the 47-minute mega-sets",
            "Ignoring the 'prod. by' tags",
            "Sidestepping the remixes of remixes",
            "Looking past the unmastered demos",
            "Checking who actually uploaded this",
            "Filtering out the podcast episodes",
            "Hunting the real upload, not the rip",
            "Searching between the airhorns and producer tags",
            "Avoiding anything described as an absolute weapon",
            "Looking past seven remixes made before breakfast",
            "Checking whether the drop ever actually arrives",
            "Navigating a light dusting of self-promotion",
            "Searching the orange wilderness",
        ],

        zvu4no: [
            "Knocking on zvu4no's door",
            "Whispering to zvu4no",
            "Pronouncing zvu4no correctly (attempt 3)",
            "Slipping zvu4no a note",
            "Catching zvu4no on a good day",
            "Decoding what zvu4no even means",
            "Politely badgering zvu4no",
            "Waiting for zvu4no to wake up",
        ],

        freemp3cloud: [
            "Coaxing FreeMp3Cloud",
            "Wringing out the cloud",
            "Catching the drips from the cloud",
            "Waiting for the cloud to clear",
            "Asking the cloud to hurry along",
            "Squeezing an MP3 from the sky",
            "Checking the forecast (cloudy, obviously)",
            "Reminding the cloud it's free",
        ],

        soulseek: [
            "Seeking souls on Soulseek",
            "Bribing the Soulseek peers",
            "Queuing politely on Soulseek",
            "Waiting for a free upload slot",
            "Befriending a stranger with good taste",
            "Joining the queue behind 38 people",
            "Hoping someone's still got their PC on",
            "Negotiating peer-to-peer diplomacy",
            "Trusting a stranger's folder names",
            "Checking who's actually sharing today",
            "Tipping the uploader (in good vibes)",
            "Communing with the Soulseek hive",
            "Dowsing for souls",
            "Knocking on computers last rebooted in 2014",
            "Borrowing music from the internet's spare bedroom",
            "Searching folders organised by one person's private logic",
            "Waiting for a peer whose uptime deserves an award",
            "Asking strangers to rummage through their hard drives",
            "Entering filenames from a less regulated age",
            "Hoping the uploader has not gone to bed",
        ],

        _default: [
            "Having a rummage",
            "Asking nicely",
            "Turning over the cushions",
            "Checking down the back of the sofa",
            "Making some calls",
            "Following a hunch",
            "Casting the net",
            "Giving it a good shake",
            "Rebuilding the library the streaming giants misplaced",
            "Looking for the album a licensing deal made disappear",
            "Undoing another triumph of shareholder value",
            "Searching for music you apparently only rented",
            "Preserving culture while quarterly earnings look elsewhere",
            "Putting ownership back where the buy button implied it was",
            "I blame Spotify for making me do this",
            "Removing music I paid for is why we're here",
        ],
    },

    empty: [
        "nothing here, sorry",
        "came up empty",
        "not a sausage",
        "drew a blank",
        "nada, zip, zilch",
        "nope, nothing",
        "all quiet on this one",
        "the cupboard's bare",
        "not a dickie bird",
        "computer says no",
        "found precisely bugger all",
        "returned with empty pockets",
        "not even a suspicious cover version",
        "nothing but the sound of distant servers",
        "searched everywhere except somewhere useful",
        "no joy, despite some very professional rummaging",
        "the trail has gone cold",
        "zero tunes, several opinions",
    ],

    // Shown when a source is still going long after the others finished. Per
    // source, with a fallback. Soulseek gets soul-themed lines for its retry.
    slow: {
        soulseek: [
            "Soulseek's taking its time seeking souls, bear with it",
            "Still seeking souls, they're a shy bunch",
            "The souls are playing hard to get, having another go",
            "Deep in the Soulseek catacombs, hang on",
            "Giving the souls a second chance to answer",
            "Souls are slow to surface, one more knock",
        ],
        _default: [
            "This one's having a proper think",
            "Still rummaging, won't be long",
            "Taking the scenic route",
            "Hang on, nearly there",
            "Just coaxing the last drop out",
        ],
    },

    // Soulseek-specific empty result, because "soulless" was right there
    soulless: [
        "came back soulless",
        "not a soul in sight",
        "no souls found, sorry",
        "the souls have left the building",
        "soulless, nothing doing",
        "couldn't find a single soul",
        "souls all out, try again later",
    ],

    timeout: [
        "took too long, moved on",
        "still faffing, left it behind",
        "off having a cup of tea",
        "ran out of patience",
        "gave up waiting",
        "stuck in traffic, carried on without it",
        "ghosted us, frankly",
        "left it to think about what it's done",
        "dragging its heels, so we left",
        "no reply, moving swiftly along",
        "asleep at the wheel",
        "took the scenic route, missed the deadline",
        "went for lunch during a simple question",
        "still loading in a more philosophical sense",
        "missed the bus and blamed the timetable",
        "became emotionally attached to the request",
        "timed out with considerable confidence",
        "wandered off midway through the conversation",
        "needed longer than civilisation could spare",
    ],

    offline: [
        "offline, skipped",
        "out to lunch",
        "closed for business",
        "not picking up",
        "gone dark, skipping",
        "having a duvet day",
        "lights off, nobody home",
        "on holiday, apparently",
        "down for the count",
        "skipped, it's having a moment",
        "currently unreachable",
        "taking a well-earned nap",
    ],

    error: [
        "had a wobble",
        "threw a strop",
        "tripped over its own feet",
        "spat the dummy",
        "fell over, dusting itself off",
        "had a little meltdown",
        "coughed up an error",
        "did not enjoy that question",
        "went a funny colour",
        "lost the plot momentarily",
        "blue-screened in spirit",
        "had a senior moment",
        "pressed the wrong button internally",
        "encountered consequences",
        "made a noise engineers dislike",
        "has submitted an incident report to itself",
        "failed in a technically interesting way",
        "found an exciting new route to nowhere",
        "tripped over a semicolon and is blaming us",
        "has entered the denial stage of debugging",
    ],
};

// =============================================================================
// Queue / Live Updates quips
// =============================================================================
// The little aside under the Queue tab's live summary line. One per state,
// picked at random whenever the state changes (not on every poll, so it
// doesn't gabble on).
//
// Structure:
//   QUEUE_QUIPS.both         -> at least one downloading and one queued
//   QUEUE_QUIPS.downloading  -> downloading, nothing waiting
//   QUEUE_QUIPS.queued       -> waiting, nothing downloading yet
//   QUEUE_QUIPS.idle         -> all caught up
//
// Same house style as SEARCH_QUIPS: British, light, no emdashes.

const QUEUE_QUIPS = {

    both: [
        "The conveyor belt is earning its keep.",
        "Two things at once. How modern.",
        "Busy, but far too polite to show it.",
        "Multitasking, cautiously optimistic.",
        "The workbench has become a production line.",
        "The machinery is humming and nobody has lost a finger.",
        "Several plates spinning, only one visibly wobbling.",
        "The queue is busy pretending this was all planned.",
        "Full steam ahead, at a responsible pressure.",
        "Work is occurring. Management is delighted.",
        "The tiny hammers have requested overtime.",
    ],

    downloading: [
        "Tiny hammers, serious business.",
        "Bits on the move, quietly.",
        "The workbench is warm.",
        "Somewhere, a tiny hammer is rather proud of itself.",
        "Progress, of the unglamorous sort.",
        "Converting electricity into music and mild warmth.",
        "A file is being persuaded into existence.",
        "Downloading with all the urgency of a council meeting.",
        "The progress bar knows where it is going.",
        "Bits are arriving in broadly the correct order.",
        "The tiny hammer union has approved this shift.",
        "Something musical is coming down the pipes.",
        "Applying patience directly to the internet.",
        "I'd still record it off the radio, if I could.",
        "Making permanent what a licensing deal made temporary.",
    ],

    queued: [
        "Forming an orderly queue, naturally.",
        "Patience: we're British, remember.",
        "Waiting its turn, ever so politely.",
        "Standing in line like civilised little files.",
        "Taking a ticket, minding its manners.",
        "Queued behind someone with a much larger trolley.",
        "Waiting patiently, while judging the track ahead.",
        "Ticket taken. Magazine selection disappointing.",
        "Currently in the foyer, pretending to read a leaflet.",
        "The files have formed a queue without being asked.",
        "Waiting for the tiny hammer to become available.",
        "Standing by with the resigned air of a commuter.",
        "Next in line, unless someone claims priority boarding.",
        "Queued because ownership now requires admin.",
    ],

    idle: [
        "The queue has put its feet up.",
        "Not a hammer stirring.",
        "The workbench has been swept.",
        "Kettle's on, nothing else is happening.",
        "All quiet on the download front.",
        "Nothing doing. Even the progress bar has gone home.",
        "The tiny hammers are in the biscuit tin.",
        "All finished. Please resist inventing more work.",
        "The machinery is off and the silence is suspicious.",
        "No queue. This level of efficiency feels unnatural.",
        "Everything is done, pending someone noticing.",
        "The workbench is spotless. Give it five minutes.",
        "Idle, but maintaining a convincing air of purpose.",
        "Your music is still here. Novel idea, apparently.",
    ],
};

// =============================================================================
// MusicBrainz lookup quips
// =============================================================================
// Shown while the Albums browser waits on a MusicBrainz artist search or
// album listing. Flat pool, one picked at random per loading state (same
// spirit as SEARCH_QUIPS.searching: pick once, don't gabble on a timer).
//
// House style: British, light, no emdashes. Be cheeky about the wait, not
// unkind about the volunteers who run the place.

const MUSICBRAINZ_QUIPS = [
    "Bothering MusicBrainz again, sorry MusicBrainz",
    "Waiting for an unpaid volunteer to feel like it",
    "A database run entirely on enthusiasm and mild guilt",
    "Reading a disambiguation longer than the album itself",
    "Untangling recordings, releases, release groups, and works, again",
    "Working out which of four identical MBIDs actually means something",
    "Being told off for asking more than once a second",
    "Waiting on an edit nobody can be bothered to vote on",
    "Checking this isn't secretly the tribute act's page",
    "Cross-referencing three spellings nobody agreed on",
    "Ignoring a bootleg someone lovingly catalogued in 2007",
    "Praying the servers survive on donations alone",
    "Consulting an encyclopaedia written by hobbyists with strong opinions",
    "Checking whether 'Various Artists' has muscled in again",
    "Reading an essay about a seven-inch single nobody bought",
    "Asking which release counts as 'canonical' this week",
    "Respecting a rate limit that assumes we have all day",
    "Double-checking the UUID hasn't wandered off since Tuesday",
    "Letting the volunteers work at their own, unhurried pace",
    "Fetching metadata catalogued with faintly alarming dedication",
    "Standing behind several thousand equally polite requests",
    "Confirming the artist isn't quietly three different artists",
];
