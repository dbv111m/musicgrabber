        // =============================================================================
        // Release Notes
        // =============================================================================
        // RELEASE_NOTES is defined in release-notes.js, loaded before this file.
        // To update release notes for a new version, edit static/release-notes.js only.

        const releaseNotesVersions = Object.keys(RELEASE_NOTES).sort((a, b) => {
            const aParts = a.split('.').map(n => parseInt(n, 10) || 0);
            const bParts = b.split('.').map(n => parseInt(n, 10) || 0);
            const maxLen = Math.max(aParts.length, bParts.length);
            for (let i = 0; i < maxLen; i++) {
                const av = aParts[i] || 0;
                const bv = bParts[i] || 0;
                if (av !== bv) return bv - av;
            }
            return 0;
        });
        let currentReleaseNotesIndex = -1;

        function updateReleaseNotesPager() {
            const olderBtn = document.getElementById('releaseNotesOlderBtn');
            const newerBtn = document.getElementById('releaseNotesNewerBtn');
            if (!olderBtn || !newerBtn) return;

            const atNewest = currentReleaseNotesIndex <= 0;
            const atOldest = currentReleaseNotesIndex >= releaseNotesVersions.length - 1;

            olderBtn.disabled = atOldest;
            newerBtn.disabled = atNewest;
        }

        function renderReleaseNotes(index) {
            if (index < 0 || index >= releaseNotesVersions.length) return;

            const version = releaseNotesVersions[index];
            const notes = RELEASE_NOTES[version];
            if (!notes) return;

            currentReleaseNotesIndex = index;

            const overlay = document.getElementById('releaseNotesOverlay');
            const titleEl = document.getElementById('releaseNotesTitle');
            const bodyEl = document.getElementById('releaseNotesBody');

            titleEl.textContent = notes.title;

            bodyEl.innerHTML = notes.sections.map(section => {
                if (section.warning) {
                    return `<div style="margin-bottom:16px; padding:12px 14px; background:rgba(245,158,11,0.12); border:1px solid rgba(245,158,11,0.4); border-radius:6px;">` +
                        `<div style="font-size:12px; font-weight:700; color:#f59e0b; margin-bottom:6px; text-transform:uppercase; letter-spacing:0.05em;">&#9888; ${escapeHtml(section.heading)}</div>` +
                        `<p style="margin:0; color:var(--text-secondary);">${escapeHtml(section.warning)}</p>` +
                        (section.items ? `<ul style="margin:8px 0 0; padding-left:18px; color:var(--text-secondary);">${section.items.map(item => `<li style="margin-bottom:4px;">${escapeHtml(item)}</li>`).join('')}</ul>` : '') +
                        `</div>`;
                }
                let html = `<div style="margin-bottom:16px;">`;
                html += `<div style="font-size:12px; font-weight:600; color:var(--text-primary); margin-bottom:6px; text-transform:uppercase; letter-spacing:0.05em;">${escapeHtml(section.heading)}</div>`;
                if (section.body) {
                    html += `<p style="margin:0; color:var(--text-secondary);">${escapeHtml(section.body)}</p>`;
                }
                if (section.items) {
                    html += `<ul style="margin:0; padding-left:18px; color:var(--text-secondary);">`;
                    html += section.items.map(item => `<li style="margin-bottom:4px;">${escapeHtml(item)}</li>`).join('');
                    html += `</ul>`;
                }
                html += `</div>`;
                return html;
            }).join('');

            updateReleaseNotesPager();
            overlay.style.display = 'flex';
        }

        function showReleaseNotes(version) {
            // Strip pre-release suffixes (-dev, -beta, -rc1, etc.) so dev builds
            // show the same notes as the release they're building toward.
            const baseVersion = (version || '').replace(/[-+].+$/, '');
            const targetIndex = releaseNotesVersions.indexOf(baseVersion);
            if (targetIndex === -1) return;
            renderReleaseNotes(targetIndex);
        }

        function showOlderReleaseNotes() {
            if (currentReleaseNotesIndex < releaseNotesVersions.length - 1) {
                renderReleaseNotes(currentReleaseNotesIndex + 1);
            }
        }

        function showNewerReleaseNotes() {
            if (currentReleaseNotesIndex > 0) {
                renderReleaseNotes(currentReleaseNotesIndex - 1);
            }
        }

        function closeReleaseNotes() {
            const overlay = document.getElementById('releaseNotesOverlay');
            overlay.style.display = 'none';
            // Store base version (strip -dev etc.) so the modal doesn't reappear for
            // the same release regardless of whether it was seen on a dev or release build.
            if (serverConfig.version) {
                localStorage.setItem('seen_version', serverConfig.version.replace(/[-+].+$/, ''));
            }
        }

        // Close on backdrop click
        document.getElementById('releaseNotesOverlay').addEventListener('click', (e) => {
            if (e.target === e.currentTarget) closeReleaseNotes();
        });

        // =============================================================================
        // Session Authentication
        // =============================================================================

        let serverConfig = {}; // Populated from /api/config at startup

        function normaliseRootPath(path) {
            path = String(path || '').trim();
            if (!path || path === '/') return '';
            return '/' + path.replace(/^\/+|\/+$/g, '');
        }

        const APP_ROOT_PATH = (() => {
            const injected = typeof window.__MUSICGRABBER_ROOT_PATH__ === 'string'
                ? window.__MUSICGRABBER_ROOT_PATH__
                : '';
            const meta = document.querySelector('meta[name="musicgrabber-root-path"]')?.content || '';
            if (injected || meta) return normaliseRootPath(injected || meta);
            const path = window.location.pathname || '/';
            return normaliseRootPath(path.replace(/\/+$/, ''));
        })();

        function withRootPath(path) {
            const value = String(path || '');
            if (!value) return APP_ROOT_PATH || '/';
            if (/^[a-z][a-z0-9+.-]*:/i.test(value) || value.startsWith('//')) return value;
            if (APP_ROOT_PATH && (value === APP_ROOT_PATH || value.startsWith(`${APP_ROOT_PATH}/`) || value.startsWith(`${APP_ROOT_PATH}?`))) {
                return value;
            }
            if (value.startsWith('/')) return `${APP_ROOT_PATH}${value}`;
            if (value.startsWith('?')) return `${APP_ROOT_PATH || ''}/${value}`;
            return APP_ROOT_PATH ? `${APP_ROOT_PATH}/${value.replace(/^\/+/, '')}` : `/${value.replace(/^\/+/, '')}`;
        }

        const missingTrackVersionsState = {
            mode: 'watched',
            jobId: '',
            playlistId: '',
            playlistName: '',
            artist: '',
            title: '',
            rowId: '',
            results: [],
        };

        function getSessionToken() {
            return localStorage.getItem('sessionToken') || '';
        }

        function setSessionToken(token) {
            if (token) localStorage.setItem('sessionToken', token);
            else localStorage.removeItem('sessionToken');
        }

        function getCurrentUser() {
            try {
                return JSON.parse(localStorage.getItem('currentUser') || 'null');
            } catch { return null; }
        }

        function setCurrentUser(user) {
            if (user) localStorage.setItem('currentUser', JSON.stringify(user));
            else localStorage.removeItem('currentUser');
        }

        function isAdmin() {
            const user = getCurrentUser();
            // In single-user mode (no users_exist), user is null but we treat as admin.
            // In session mode, check the role.
            return !user || user.role === 'admin';
        }

        function isPeon() {
            const user = getCurrentUser();
            return !!(user && user.role === 'peon');
        }

        // Namespaced localStorage key, keeps per-user preferences separate on shared browsers.
        // Session-global keys (theme, seen_version, sessionToken, etc.) are NOT namespaced.
        function userStorageKey(key) {
            const user = getCurrentUser();
            return user && user.id ? `mg_${user.id}_${key}` : `mg_${key}`;
        }

        function buildJobDownloadPath(jobId) {
            return withRootPath(`/api/jobs/${encodeURIComponent(String(jobId || ''))}/download`);
        }

        async function getJobDownloadUrl(jobId) {
            if (!serverConfig?.users_exist) return buildJobDownloadPath(jobId);
            const resp = await apiFetch('/api/auth/download-token', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ job_id: String(jobId || '') }),
            });
            if (!resp.ok) {
                const data = await resp.json().catch(() => ({}));
                throw new Error(data.detail || 'Unable to issue download token');
            }
            const data = await resp.json();
            return data.url || buildJobDownloadPath(jobId);
        }

        async function saveJobToDevice(jobId) {
            try {
                const url = await getJobDownloadUrl(jobId);
                const a = document.createElement('a');
                a.href = withRootPath(url);
                a.download = '';
                a.rel = 'noopener';
                a.style.display = 'none';
                document.body.appendChild(a);
                a.click();
                a.remove();
            } catch (error) {
                showToast('Failed to start download', true);
            }
        }

        async function apiFetch(url, options = {}) {
            const token = getSessionToken();
            const headers = { ...options.headers };
            if (token) {
                headers['Authorization'] = `Bearer ${token}`;
            }
            const response = await fetch(withRootPath(url), { ...options, headers });

            if (response.status === 401) {
                // Session expired or invalid; clear local state and show login screen
                if (token === getSessionToken() && serverConfig && serverConfig.users_exist) {
                    setSessionToken(null);
                    setCurrentUser(null);
                    showLoginScreen();
                }
                throw new Error('Authentication required');
            }

            if (response.status === 429) {
                const data = await response.json().catch(() => ({}));
                throw new Error(data.detail || 'Rate limit exceeded');
            }

            return response;
        }

        // =============================================================================
        // Login / Logout / Password Change
        // =============================================================================

        function showLoginScreen() {
            document.getElementById('loginScreen').style.display = 'flex';
            document.getElementById('app').style.display = 'none';
            document.getElementById('loginError').style.display = 'none';
            document.getElementById('loginPassword').value = '';
            setTimeout(() => document.getElementById('loginUsername').focus(), 50);
        }

        function hideLoginScreen() {
            document.getElementById('loginScreen').style.display = 'none';
            document.getElementById('app').style.display = '';
        }

        async function doLogin() {
            const username = document.getElementById('loginUsername').value.trim();
            const password = document.getElementById('loginPassword').value;
            const errorDiv = document.getElementById('loginError');
            const btn = document.getElementById('loginBtn');

            if (!username || !password) {
                errorDiv.textContent = 'Please enter username and password.';
                errorDiv.style.display = 'block';
                return;
            }

            btn.disabled = true;
            errorDiv.style.display = 'none';

            try {
                const resp = await fetch(withRootPath('/api/auth/login'), {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ username, password }),
                });

                if (!resp.ok) {
                    const data = await resp.json().catch(() => ({}));
                    errorDiv.textContent = data.detail || 'Invalid username or password.';
                    errorDiv.style.display = 'block';
                    return;
                }

                const data = await resp.json();
                setSessionToken(data.token);
                setCurrentUser(data.user);
                hideLoginScreen();

                if (data.user.force_password_change) {
                    showChangePasswordScreen(true);
                    return;
                }

                // Reinitialise the app now that we're logged in
                location.reload();
            } catch (e) {
                errorDiv.textContent = 'Login failed. Please try again.';
                errorDiv.style.display = 'block';
            } finally {
                btn.disabled = false;
            }
        }

        async function doLogout() {
            try {
                const token = getSessionToken();
                if (token) {
                    await fetch(withRootPath('/api/auth/logout'), {
                        method: 'POST',
                        headers: { 'Authorization': `Bearer ${token}` },
                    });
                }
            } catch {}
            setSessionToken(null);
            setCurrentUser(null);
            location.reload();
        }

        function showChangePasswordScreen(forced = false) {
            document.getElementById('changePasswordScreen').style.display = 'flex';
            document.getElementById('app').style.display = 'none';
            document.getElementById('changePasswordForced').style.display = forced ? 'block' : 'none';
            document.getElementById('changePasswordError').style.display = 'none';
        }

        function hideChangePasswordScreen() {
            document.getElementById('changePasswordScreen').style.display = 'none';
            document.getElementById('app').style.display = '';
        }

        async function doChangePassword() {
            const currentPw = document.getElementById('changePasswordCurrent').value;
            const newPw = document.getElementById('changePasswordNew').value;
            const confirmPw = document.getElementById('changePasswordConfirm').value;
            const errorDiv = document.getElementById('changePasswordError');

            if (newPw !== confirmPw) {
                errorDiv.textContent = 'Passwords do not match.';
                errorDiv.style.display = 'block';
                return;
            }
            if (newPw.length < 8) {
                errorDiv.textContent = 'Password must be at least 8 characters.';
                errorDiv.style.display = 'block';
                return;
            }

            try {
                const resp = await apiFetch('/api/auth/password', {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ current_password: currentPw, new_password: newPw }),
                });
                if (!resp.ok) {
                    const data = await resp.json().catch(() => ({}));
                    errorDiv.textContent = data.detail || 'Failed to change password.';
                    errorDiv.style.display = 'block';
                    return;
                }
                // Update stored user to clear force_password_change
                const user = getCurrentUser();
                if (user) {
                    user.force_password_change = false;
                    setCurrentUser(user);
                }
                hideChangePasswordScreen();
                location.reload();
            } catch (e) {
                errorDiv.textContent = 'Error changing password.';
                errorDiv.style.display = 'block';
            }
        }

        // =============================================================================
        // Theme Toggle - Day and Night Modes
        // =============================================================================

        function getTheme() {
            return localStorage.getItem('theme') || 'dark';
        }

        function setTheme(theme) {
            document.documentElement.setAttribute('data-theme', theme);
            localStorage.setItem('theme', theme);
            const btn = document.getElementById('themeToggle');
            if (btn) btn.innerHTML = theme === 'dark' ? '<i class="fa-solid fa-moon"></i>' : '<i class="fa-solid fa-sun"></i>';
        }

        // Apply saved theme immediately
        setTheme(getTheme());

        document.getElementById('themeToggle').addEventListener('click', () => {
            setTheme(getTheme() === 'dark' ? 'light' : 'dark');
        });

        // =============================================================================
        // DOM Elements
        // =============================================================================

        const searchInput = document.getElementById('searchInput');
        const searchBtn = document.getElementById('searchBtn');
        const searchClearBtn = document.getElementById('searchClearBtn');

        function setSearchValue(val) {
            searchInput.value = val;
            searchClearBtn.style.display = val ? '' : 'none';
        }
        const searchHistory = document.getElementById('searchHistory');
        const relatedSuggestions = document.getElementById('relatedSuggestions');
        const resultsTab = document.getElementById('resultsTab');
        const bulkTabContainer = document.getElementById('bulkTabContainer');
        const artistsTabContainer = document.getElementById('artistsTabContainer');
        const bulkInput = document.getElementById('bulkInput');
        const bulkImportBtn = document.getElementById('bulkImportBtn');
        const bulkResults = document.getElementById('bulkResults');
        const bulkImportJobsContainer = document.getElementById('bulkImportJobsContainer');
        const fileUpload = document.getElementById('fileUpload');
        const fileName = document.getElementById('fileName');
        const spotifyUrlInput = document.getElementById('spotifyUrlInput');
        const playlistServicesHint = document.getElementById('playlistServicesHint');
        const playlistServicesTooltip = document.getElementById('playlistServicesTooltip');
        const fetchSpotifyBtn = document.getElementById('fetchSpotifyBtn');
        const spotifyError = document.getElementById('spotifyError');
        const albumMatchPanel = document.getElementById('albumMatchPanel');
        const longformSplitPanel = document.getElementById('longformSplitPanel');
        const queueTabContainer = document.getElementById('queueTabContainer');
        const queueTab = document.getElementById('queueTab');
        const queueLiveSummary = document.getElementById('queueLiveSummary');
        const queueHistorySort = document.getElementById('queueHistorySort');
        const clearQueueBtn = document.getElementById('clearQueueBtn');
        const resetStatsBtn = document.getElementById('resetStatsBtn');
        const watchedTabContainer = document.getElementById('watchedTabContainer');
        const watchedList = document.getElementById('watchedList');
        const watchedUrlInput = document.getElementById('watchedUrlInput');
        const addWatchedBtn = document.getElementById('addWatchedBtn');
        const watchedIntervalSelect = document.getElementById('watchedIntervalSelect');
        const watchedError = document.getElementById('watchedError');
        const refreshAllWatchedBtn = document.getElementById('refreshAllWatchedBtn');
        const watchedScheduleInfo = document.getElementById('watchedScheduleInfo');
        const watchedConvertAudio = document.getElementById('watchedConvertAudio');
        const toast = document.getElementById('toast');
        const tabs = document.querySelectorAll('.tab');
        const lineCounter = document.getElementById('lineCounter');
        const statsTabContainer = document.getElementById('statsTabContainer');
        const statsContent = document.getElementById('statsContent');
        const settingsTabContainer = document.getElementById('settingsTabContainer');
        const createPlaylistCheckbox = document.getElementById('createPlaylistCheckbox');
        const playlistNameInput = document.getElementById('playlistNameInput');
        const convertAudioCheckbox = document.getElementById('convertAudio');
        let watchedConversionTouched = false;

        // =============================================================================
        // Destination Picker (unified "Add to..." for Playlist and Album)
        // =============================================================================

        let _playlists = [];
        let _destinationMode = null; // null | 'playlist' | 'album'
        let _albumDirArtist = null;
        let _albumDirAlbum = null;
        let _albumInfo = null; // response from /api/albums/dirs/{artist}/{album}/info

        let playlistScanTimer = null;
        let playlistScanBusy = false;
        async function loadPlaylists() {
            if (playlistScanBusy) return;
            playlistScanBusy = true;
            clearTimeout(playlistScanTimer);
            try {
                const resp = await apiFetch('/api/playlists');
                if (!resp.ok) {
                    console.warn('GET /api/playlists returned', resp.status);
                    return;
                }
                const data = await resp.json();
                _playlists = data.playlists || [];
                _populatePlaylistSelector();
                const sel = document.getElementById('playlistSelectorInput');
                if (sel && data.scan_state !== 'ready') {
                    const status = document.createElement('option');
                    status.disabled = true;
                    status.textContent = data.scan_state === 'error'
                        ? 'Library playlist scan failed; try again shortly'
                        : 'Scanning library playlists…';
                    sel.appendChild(status);
                }
                if (data.scan_state === 'refreshing' && document.getElementById('app').style.display !== 'none') {
                    playlistScanTimer = setTimeout(loadPlaylists, 3000);
                }
            } catch (e) {
                console.warn('loadPlaylists failed:', e);
            } finally {
                playlistScanBusy = false;
            }
        }

        function _populatePlaylistSelector() {
            const sel = document.getElementById('playlistSelectorInput');
            if (!sel) return;
            const current = sel.value;
            while (sel.options.length > 1) sel.remove(1);
            for (const pl of _playlists) {
                const opt = document.createElement('option');
                opt.value = pl.name;
                opt.textContent = pl.is_watched ? `${pl.name} (watched)` : pl.name;
                opt.dataset.isWatched = pl.is_watched ? '1' : '0';
                opt.dataset.syncMode = pl.sync_mode || 'append';
                sel.appendChild(opt);
            }
            const newOpt = document.createElement('option');
            newOpt.value = '__new__';
            newOpt.textContent = '+ New playlist...';
            sel.appendChild(newOpt);
            if (current && [...sel.options].some(o => o.value === current)) {
                sel.value = current;
            }
        }

        function getSelectedPlaylist() {
            if (_destinationMode !== 'playlist') return null;
            const panel = document.getElementById('playlistSelector');
            const sel = document.getElementById('playlistSelectorInput');
            if (!sel || !sel.value || sel.value === '__new__' || !panel || panel.style.display === 'none') return null;
            const opt = sel.options[sel.selectedIndex];
            return { name: sel.value, is_watched: opt && opt.dataset.isWatched === '1' };
        }

        function _updatePlaylistSelectorWarning() {
            const sel = document.getElementById('playlistSelectorInput');
            const warn = document.getElementById('playlistSelectorWarn');
            if (!warn || !sel) return;
            const opt = sel.options[sel.selectedIndex];
            const isMirror = opt && opt.dataset.isWatched === '1' && opt.dataset.syncMode === 'mirror';
            warn.style.display = isMirror ? 'inline' : 'none';
        }

        function getSelectedAlbumRoute() {
            if (_destinationMode !== 'album') return null;
            if (!_albumDirArtist || !_albumDirAlbum) return null;

            const base = {
                album_artist: _albumDirArtist,
                album_name: _albumDirAlbum,
                release_mbid: _albumInfo ? _albumInfo.release_mbid : null,
                track_total: _albumInfo ? ((_albumInfo.tracks || []).length || null) : null,
            };

            if (!_albumInfo || !_albumInfo.release_mbid) {
                // Folder routing only, no MBID so no track metadata enrichment
                return { ...base, mode: 'no_mbid' };
            }

            const modeSel = document.getElementById('albumRouteMode');
            const trackSel = document.getElementById('albumRouteTrackSelect');
            const mode = modeSel ? modeSel.value : 'auto';

            if (mode !== 'manual') {
                return { ...base, mode: 'auto' };
            }
            if (!trackSel || trackSel.value === '') return null;

            const idx = Number(trackSel.value);
            const track = (_albumInfo.tracks || [])[idx];
            if (!track) return null;
            let trackNum = null;
            if (track.position && /^\d+$/.test(String(track.position).trim())) {
                trackNum = Number(String(track.position).trim());
            } else {
                trackNum = idx + 1;
            }
            return { ...base, mode: 'manual', track_title: track.title, track_number: trackNum };
        }

        function _resetDestination() {
            _destinationMode = null;
            _albumDirArtist = null;
            _albumDirAlbum = null;
            _albumInfo = null;
            const toggle = document.getElementById('destinationPickerToggle');
            const modePanel = document.getElementById('destinationModePanel');
            const playlistPanel = document.getElementById('playlistSelector');
            const albumPanel = document.getElementById('albumRoutePanel');
            if (toggle) { toggle.textContent = '+ Add to...'; toggle.classList.remove('active'); }
            if (modePanel) modePanel.style.display = 'none';
            if (playlistPanel) playlistPanel.style.display = 'none';
            if (albumPanel) albumPanel.style.display = 'none';
            const sel = document.getElementById('playlistSelectorInput');
            if (sel) sel.value = '';
            _updatePlaylistSelectorWarning();
        }

        async function _loadAlbumArtists() {
            const sel = document.getElementById('albumArtistSelect');
            if (!sel) return;
            sel.innerHTML = '<option value="">Loading...</option>';
            try {
                const resp = await apiFetch('/api/albums/dirs');
                if (!resp.ok) { sel.innerHTML = '<option value="">Error loading artists</option>'; return; }
                const data = await resp.json();
                sel.innerHTML = '<option value="">Select artist...</option>';
                for (const a of (data.artists || [])) {
                    const opt = document.createElement('option');
                    opt.value = a;
                    opt.textContent = a;
                    sel.appendChild(opt);
                }
                if (!(data.artists || []).length) {
                    sel.innerHTML = '<option value="">No albums on disk yet</option>';
                }
            } catch (e) {
                sel.innerHTML = '<option value="">Error loading artists</option>';
            }
        }

        async function _loadAlbumFolders(artist) {
            const sel = document.getElementById('albumFolderSelect');
            const statusEl = document.getElementById('albumRouteStatus');
            const modeSel = document.getElementById('albumRouteMode');
            const trackSel = document.getElementById('albumRouteTrackSelect');
            const warnEl = document.getElementById('albumRouteWarn');
            _albumInfo = null;
            _albumDirAlbum = null;
            if (statusEl) statusEl.style.display = 'none';
            if (modeSel) modeSel.style.display = 'none';
            if (trackSel) trackSel.style.display = 'none';
            if (warnEl) warnEl.style.display = 'none';
            if (!sel) return;
            sel.innerHTML = '<option value="">Loading...</option>';
            sel.style.display = '';
            try {
                const resp = await apiFetch(`/api/albums/dirs/${encodeURIComponent(artist)}`);
                if (!resp.ok) { sel.innerHTML = '<option value="">Error loading albums</option>'; return; }
                const data = await resp.json();
                sel.innerHTML = '<option value="">Select album...</option>';
                for (const al of (data.albums || [])) {
                    const opt = document.createElement('option');
                    opt.value = al;
                    opt.textContent = al;
                    sel.appendChild(opt);
                }
            } catch (e) {
                sel.innerHTML = '<option value="">Error loading albums</option>';
            }
        }

        async function _loadAlbumInfo(artist, album) {
            const statusEl = document.getElementById('albumRouteStatus');
            const modeSel = document.getElementById('albumRouteMode');
            const trackSel = document.getElementById('albumRouteTrackSelect');
            const warnEl = document.getElementById('albumRouteWarn');
            _albumDirArtist = artist;
            _albumDirAlbum = album;
            _albumInfo = null;
            if (statusEl) { statusEl.textContent = 'Loading...'; statusEl.style.display = ''; }
            if (modeSel) modeSel.style.display = 'none';
            if (trackSel) trackSel.style.display = 'none';
            if (warnEl) warnEl.style.display = 'none';
            try {
                const resp = await apiFetch(
                    `/api/albums/dirs/${encodeURIComponent(artist)}/${encodeURIComponent(album)}/info`
                );
                const data = await resp.json();
                if (data.found && data.release_mbid) {
                    _albumInfo = data;
                    if (statusEl) statusEl.textContent = `${data.artist} / ${data.album}`;
                    const tracks = data.tracks || [];
                    if (trackSel) {
                        trackSel.innerHTML = tracks.map((t, idx) => {
                            const num = t.position ? `${t.position}. ` : `${idx + 1}. `;
                            return `<option value="${idx}">${escapeHtml(num + (t.title || 'Unknown'))}</option>`;
                        }).join('');
                    }
                    if (modeSel) { modeSel.style.display = ''; modeSel.value = 'auto'; }
                    if (trackSel) trackSel.style.display = 'none';
                } else {
                    if (statusEl) statusEl.textContent = `${artist} / ${album}`;
                    if (warnEl) {
                        warnEl.textContent = 'Album info not found - track metadata will not be embedded.';
                        warnEl.style.display = '';
                    }
                }
            } catch (e) {
                if (statusEl) statusEl.textContent = 'Error loading album info.';
            }
        }

        function _initPlaylistPanelInternals() {
            const sel = document.getElementById('playlistSelectorInput');
            const clearBtn = document.getElementById('playlistSelectorClear');
            const newNameInput = document.getElementById('playlistNewNameInput');
            const newNameConfirm = document.getElementById('playlistNewNameConfirm');
            if (!sel) return;

            function _showNewPlaylistInput() {
                if (newNameInput) { newNameInput.style.display = ''; newNameInput.value = ''; newNameInput.focus(); }
                if (newNameConfirm) newNameConfirm.style.display = '';
            }
            function _hideNewPlaylistInput() {
                if (newNameInput) newNameInput.style.display = 'none';
                if (newNameConfirm) newNameConfirm.style.display = 'none';
            }
            function _confirmNewPlaylist() {
                const name = (newNameInput ? newNameInput.value : '').trim();
                if (!name) { newNameInput && newNameInput.focus(); return; }
                const existing = [...sel.options].find(o => o.value.toLowerCase() === name.toLowerCase() && o.value !== '__new__');
                if (existing) { sel.value = existing.value; _hideNewPlaylistInput(); _updatePlaylistSelectorWarning(); return; }
                const newOpt = document.createElement('option');
                newOpt.value = name;
                newOpt.textContent = name;
                newOpt.dataset.isWatched = '0';
                const sentinel = [...sel.options].find(o => o.value === '__new__');
                sel.insertBefore(newOpt, sentinel || null);
                sel.value = name;
                _hideNewPlaylistInput();
                _updatePlaylistSelectorWarning();
            }

            sel.addEventListener('change', () => {
                if (sel.value === '__new__') _showNewPlaylistInput();
                else { _hideNewPlaylistInput(); _updatePlaylistSelectorWarning(); }
            });
            if (newNameConfirm) newNameConfirm.addEventListener('click', _confirmNewPlaylist);
            if (newNameInput) {
                newNameInput.addEventListener('keydown', (e) => {
                    if (e.key === 'Enter') { e.preventDefault(); _confirmNewPlaylist(); }
                    if (e.key === 'Escape') { sel.value = ''; _hideNewPlaylistInput(); _updatePlaylistSelectorWarning(); }
                });
            }
            if (clearBtn) clearBtn.addEventListener('click', _resetDestination);
        }

        function _initAlbumPanelInternals() {
            const artistSel = document.getElementById('albumArtistSelect');
            const albumSel = document.getElementById('albumFolderSelect');
            const modeSel = document.getElementById('albumRouteMode');
            const trackSel = document.getElementById('albumRouteTrackSelect');
            const clearBtn = document.getElementById('albumRouteClear');

            if (artistSel) {
                artistSel.addEventListener('change', () => {
                    const artist = artistSel.value;
                    if (artist) _loadAlbumFolders(artist);
                    else {
                        if (albumSel) { albumSel.innerHTML = '<option value="">Select album...</option>'; albumSel.style.display = 'none'; }
                        const statusEl = document.getElementById('albumRouteStatus');
                        if (statusEl) statusEl.style.display = 'none';
                        if (modeSel) modeSel.style.display = 'none';
                        if (trackSel) trackSel.style.display = 'none';
                        _albumDirArtist = null; _albumDirAlbum = null; _albumInfo = null;
                    }
                });
            }
            if (albumSel) {
                albumSel.addEventListener('change', () => {
                    const album = albumSel.value;
                    if (album && artistSel && artistSel.value) _loadAlbumInfo(artistSel.value, album);
                });
            }
            if (modeSel) {
                modeSel.addEventListener('change', () => {
                    if (trackSel) trackSel.style.display = modeSel.value === 'manual' ? '' : 'none';
                });
            }
            if (clearBtn) clearBtn.addEventListener('click', _resetDestination);
        }

        function initDestinationPicker() {
            const toggle = document.getElementById('destinationPickerToggle');
            const modePanel = document.getElementById('destinationModePanel');
            const playlistPanel = document.getElementById('playlistSelector');
            const albumPanel = document.getElementById('albumRoutePanel');
            if (!toggle) return;

            toggle.addEventListener('click', () => {
                if (_destinationMode !== null) { _resetDestination(); return; }
                if (modePanel) modePanel.style.display = 'flex';
                toggle.classList.add('active');
            });

            const destModePlaylist = document.getElementById('destModePlaylist');
            if (destModePlaylist) {
                destModePlaylist.addEventListener('click', () => {
                    _destinationMode = 'playlist';
                    if (modePanel) modePanel.style.display = 'none';
                    if (playlistPanel) playlistPanel.style.display = 'flex';
                    toggle.textContent = 'Adding to playlist';
                    loadPlaylists();
                    const sel = document.getElementById('playlistSelectorInput');
                    if (sel) sel.focus();
                });
            }

            const destModeAlbum = document.getElementById('destModeAlbum');
            if (destModeAlbum) {
                destModeAlbum.addEventListener('click', () => {
                    _destinationMode = 'album';
                    if (modePanel) modePanel.style.display = 'none';
                    if (albumPanel) albumPanel.style.display = 'flex';
                    toggle.textContent = 'Adding to album';
                    _loadAlbumArtists();
                });
            }

            const pickerClear = document.getElementById('destinationPickerClear');
            if (pickerClear) pickerClear.addEventListener('click', _resetDestination);

            _initPlaylistPanelInternals();
            _initAlbumPanelInternals();
        }

        // audioFormat tracks which format to use when conversion is on ("flac", "alac", "opus", or "mp3")
        let audioFormat = 'opus';

        function conversionChoiceLabel(enabled) {
            if (!enabled) return 'Keep source';
            const labels = { flac: 'FLAC', alac: 'ALAC', opus: 'Opus', mp3: 'MP3' };
            return `Convert to ${labels[audioFormat] || 'Opus'}`;
        }

        function updateConversionLabels() {
            // Text only; the warning triangle is a sibling element and would not
            // survive being written over.
            const headerText = document.getElementById('headerFormatText');
            if (headerText) headerText.textContent = conversionChoiceLabel(convertAudioCheckbox.checked);

            const watchedLabel = document.getElementById('watchedFormatLabel');
            if (watchedLabel && watchedConvertAudio) {
                watchedLabel.textContent = conversionChoiceLabel(watchedConvertAudio.checked);
            }

            const artistLabel = document.getElementById('artistFlacLabel');
            const artistCheckbox = document.getElementById('artistConvertAudio');
            if (artistLabel && artistCheckbox) {
                artistLabel.textContent = conversionChoiceLabel(artistCheckbox.checked);
            }

            updateLossyConversionWarning();
        }

        function conversionTargetIsLossy() {
            if (audioFormat === 'opus' || audioFormat === 'mp3') return true;
            if (audioFormat === 'alac') {
                return (document.getElementById('settingAlacBitrate')?.value || 'lossless') !== 'lossless';
            }
            return false;
        }

        function conversionTargetName() {
            const labels = { flac: 'FLAC', alac: 'ALAC', opus: 'Opus', mp3: 'MP3' };
            if (audioFormat === 'alac' && conversionTargetIsLossy()) return 'AAC in an M4A container';
            return labels[audioFormat] || 'Opus';
        }

        function updateLossyConversionWarning() {
            const warning = document.getElementById('lossyConversionWarning');
            const conversionEnabled = document.getElementById('settingDefaultConvertAudio')?.checked ?? false;
            const message = `Warning: converting a lossless source to ${conversionTargetName()} is a quality downgrade. Choose Keep source to preserve the original audio.`;
            if (warning) {
                warning.textContent = message;
                warning.style.display = conversionEnabled && conversionTargetIsLossy() ? 'block' : 'none';
            }

            // Only meaningful when there's a lossy target to potentially skip
            // re-encoding into; a lossless target (FLAC, real ALAC) has nothing
            // for this toggle to do.
            const losslessOnlyRow = document.getElementById('convertLosslessOnlyRow');
            if (losslessOnlyRow) {
                losslessOnlyRow.style.display = conversionEnabled && conversionTargetIsLossy() ? 'block' : 'none';
            }

            const headerControl = document.querySelector('.header-format-label');
            const headerWouldDowngrade = convertAudioCheckbox?.checked && conversionTargetIsLossy();
            // The label itself does the warning: amber, bold, and wearing a triangle.
            // A separate badge simply would not fit next to everything else up there.
            document.getElementById('headerFormatLabel')?.classList.toggle('is-lossy', !!headerWouldDowngrade);
            if (headerControl) {
                headerControl.title = headerWouldDowngrade
                    ? message
                    : 'Choose whether downloads keep their source format or are converted to the format selected in Settings.';
            }
        }

        function setDefaultConversionMode(enabled) {
            const input = document.getElementById('settingDefaultConvertAudio');
            if (!input) return;
            if (input.disabled && input.checked !== Boolean(enabled)) return;

            input.checked = Boolean(enabled);

            const keepBtn = document.getElementById('conversionModeKeep');
            const convertBtn = document.getElementById('conversionModeConvert');
            if (keepBtn) {
                keepBtn.classList.toggle('active', !input.checked);
                keepBtn.disabled = input.disabled;
            }
            if (convertBtn) {
                convertBtn.classList.toggle('active', input.checked);
                convertBtn.disabled = input.disabled;
            }

            const targets = document.getElementById('conversionTargetOptions');
            if (targets) targets.style.display = input.checked ? 'flex' : 'none';

            // Format-specific notes and quality rows should not remain visible
            // while "Keep source" is selected.
            setAudioFormat(audioFormat);
        }

        function setAudioFormat(format) {
            audioFormat = ['flac', 'alac', 'opus', 'mp3'].includes(format) ? format : 'opus';

            const btnFlac = document.getElementById('formatBtnFlac');
            const btnAlac = document.getElementById('formatBtnAlac');
            const btnOpus = document.getElementById('formatBtnOpus');
            const btnMp3  = document.getElementById('formatBtnMp3');
            if (btnFlac) btnFlac.classList.toggle('active', audioFormat === 'flac');
            if (btnAlac) btnAlac.classList.toggle('active', audioFormat === 'alac');
            if (btnOpus) btnOpus.classList.toggle('active', audioFormat === 'opus');
            if (btnMp3)  btnMp3.classList.toggle('active',  audioFormat === 'mp3');

            updateConversionLabels();

            const conversionEnabled = document.getElementById('settingDefaultConvertAudio')?.checked ?? false;
            const alacNote = document.getElementById('alacFormatNote');
            if (alacNote) alacNote.style.display = conversionEnabled && audioFormat === 'alac' ? 'block' : 'none';
            const mp3Note = document.getElementById('mp3FormatNote');
            if (mp3Note) mp3Note.style.display = conversionEnabled && audioFormat === 'mp3' ? 'block' : 'none';

            // Show quality sub-rows only for the relevant format
            const mp3QualityRow = document.getElementById('mp3QualityRow');
            if (mp3QualityRow) mp3QualityRow.style.display = conversionEnabled && audioFormat === 'mp3' ? '' : 'none';
            const opusQualityRow = document.getElementById('opusQualityRow');
            if (opusQualityRow) opusQualityRow.style.display = conversionEnabled && audioFormat === 'opus' ? '' : 'none';
            const alacQualityRow = document.getElementById('alacQualityRow');
            if (alacQualityRow) alacQualityRow.style.display = conversionEnabled && audioFormat === 'alac' ? '' : 'none';

            // Keep hidden input in sync so settings save picks it up
            const hiddenInput = document.getElementById('settingAudioFormat');
            if (hiddenInput) hiddenInput.value = audioFormat;

            localStorage.setItem(userStorageKey('audioFormat'), audioFormat);
        }

        function setMp3Bitrate(val) {
            const valid = ['v2', 'v0', '320k', '256k', '192k', '128k'];
            if (!valid.includes(val)) val = 'v2';
            const ids = { v2: 'mp3QualityBtnV2', v0: 'mp3QualityBtnV0', '320k': 'mp3QualityBtn320',
                          '256k': 'mp3QualityBtn256', '192k': 'mp3QualityBtn192', '128k': 'mp3QualityBtn128' };
            for (const [k, id] of Object.entries(ids)) {
                const btn = document.getElementById(id);
                if (btn) btn.classList.toggle('active', k === val);
            }
            const input = document.getElementById('settingMp3Bitrate');
            if (input) input.value = val;
            localStorage.setItem(userStorageKey('mp3Bitrate'), val);
        }

        function setOpusBitrate(val) {
            const valid = ['256k', '192k', '128k', '96k'];
            if (!valid.includes(val)) val = '256k';
            const ids = { '256k': 'opusQualityBtn256',
                          '192k': 'opusQualityBtn192', '128k': 'opusQualityBtn128', '96k': 'opusQualityBtn96' };
            for (const [k, id] of Object.entries(ids)) {
                const btn = document.getElementById(id);
                if (btn) btn.classList.toggle('active', k === val);
            }
            const input = document.getElementById('settingOpusBitrate');
            if (input) input.value = val;
            localStorage.setItem(userStorageKey('opusBitrate'), val);
        }

        function setAlacBitrate(val) {
            const valid = ['lossless', '320k', '256k', '192k', '128k'];
            if (!valid.includes(val)) val = 'lossless';
            const ids = { 'lossless': 'alacQualityBtnLossless', '320k': 'alacQualityBtn320',
                          '256k': 'alacQualityBtn256', '192k': 'alacQualityBtn192', '128k': 'alacQualityBtn128' };
            for (const [k, id] of Object.entries(ids)) {
                const btn = document.getElementById(id);
                if (btn) btn.classList.toggle('active', k === val);
            }
            const input = document.getElementById('settingAlacBitrate');
            if (input) input.value = val;
            localStorage.setItem(userStorageKey('alacBitrate'), val);
            updateLossyConversionWarning();
        }

        const versionLabel = document.getElementById('versionLabel');
        const PLAYLIST_SERVICES = ["Spotify", "YouTube", "Apple Music", "Amazon Music", "SoundCloud", "ListenBrainz"];

        function renderPlaylistServicesText() {
            const text = PLAYLIST_SERVICES.join(", ");
            if (playlistServicesHint) {
                playlistServicesHint.textContent = `Supported services: ${text}`;
            }
            if (playlistServicesTooltip) {
                playlistServicesTooltip.title = `Supported services: ${text}`;
            }
        }

        renderPlaylistServicesText();

        // Dismiss the pure-HTML boot panel from index.html. Called from initApp's
        // finally block, so it goes whether we land on the app, the login screen,
        // or a config fetch that fell over.
        function dismissBootPanel() {
            document.getElementById('bootPanel')?.remove();
        }

        // Load config from server then handle auth and app initialisation
        (async function initApp() {
          try {
            let config = null;
            try {
                const resp = await fetch(withRootPath('/api/config'));
                if (resp.ok) config = await resp.json();
            } catch {}

            if (config) {
                serverConfig = config;

                // Set version label
                if (config.version) {
                    versionLabel.textContent = `v${config.version}`;
                    // Show release notes once when the version changes (new install or update).
                    // Compare base versions (strip -dev etc.) so dev builds trigger correctly.
                    const baseVersion = config.version.replace(/[-+].+$/, '');
                    const seenVersion = localStorage.getItem('seen_version');
                    if (seenVersion !== baseVersion && RELEASE_NOTES[baseVersion]) {
                        showReleaseNotes(config.version);
                    }
                }

                // Set convert on/off from server if not saved locally
                if (localStorage.getItem(userStorageKey('convertAudio')) === null && typeof config.default_convert_audio === 'boolean') {
                    convertAudioCheckbox.checked = config.default_convert_audio;
                    if (watchedConvertAudio && !watchedConversionTouched) {
                        watchedConvertAudio.checked = config.default_convert_audio;
                    }
                    updateConversionLabels();
                }

                // Set audio format picker from server if not saved locally
                if (localStorage.getItem(userStorageKey('audioFormat')) === null && config.audio_format) {
                    setAudioFormat(config.audio_format);
                }

                // Multi-user: check if we need a login screen
                if (config.users_exist) {
                    const token = getSessionToken();
                    if (!token) {
                        showLoginScreen();
                        return;
                    }
                    // Validate existing session
                    try {
                        const meResp = await apiFetch('/api/auth/me');
                        if (!meResp.ok) {
                            setSessionToken(null);
                            setCurrentUser(null);
                            showLoginScreen();
                            return;
                        }
                        const user = await meResp.json();
                        setCurrentUser(user);
                        if (user.force_password_change) {
                            showChangePasswordScreen(true); // forced
                            return;
                        }
                    } catch {
                        showLoginScreen();
                        return;
                    }
                }

                // Show warning if music directory isn't mounted
                if (config.volume_mounted === false) {
                    showVolumeMountWarning();
                }

                // Show Playlists folder toggle in watched playlist add form if configured
                if (config.playlists_subdir) {
                    const toggle = document.getElementById('watchedPlaylistsDirToggle');
                    if (toggle) toggle.style.display = 'flex';
                    const watchedUsePlaylistsDir = document.getElementById('watchedUsePlaylistsDir');
                    if (watchedUsePlaylistsDir) watchedUsePlaylistsDir.checked = true;
                }
            }

            // Single-user mode OR successful session, apply role-based UI
            applyUserRoleToUI();
            populateSourceChips();
            startSourceHealthPolling();
            if (config) loadPlaylists();
            reattachBulkImportIfActive();
          } finally {
            dismissBootPanel();
          }
        })();

        // Restore convert on/off from localStorage (namespaced per user)
        const legacyConvertPref = localStorage.getItem(userStorageKey('convertToFlac'));
        const savedConvertPref = localStorage.getItem(userStorageKey('convertAudio')) ?? legacyConvertPref;
        if (savedConvertPref !== null) {
            convertAudioCheckbox.checked = savedConvertPref === 'true';
            if (legacyConvertPref !== null) {
                localStorage.setItem(userStorageKey('convertAudio'), legacyConvertPref);
                localStorage.removeItem(userStorageKey('convertToFlac'));
            }
        }

        // Restore audio format picker from localStorage (namespaced per user)
        const savedAudioFormat = localStorage.getItem(userStorageKey('audioFormat'));
        if (savedAudioFormat) {
            setAudioFormat(savedAudioFormat);
        }
        updateConversionLabels();

        if (watchedConvertAudio) {
            watchedConvertAudio.addEventListener('change', () => {
                watchedConversionTouched = true;
                updateConversionLabels();
            });
        }
        const artistConvertAudio = document.getElementById('artistConvertAudio');
        if (artistConvertAudio) {
            artistConvertAudio.addEventListener('change', updateConversionLabels);
        }

        // Save convert preference when header toggle is flipped
        convertAudioCheckbox.addEventListener('change', () => {
            localStorage.setItem(userStorageKey('convertAudio'), convertAudioCheckbox.checked);
            if (watchedConvertAudio && !watchedConversionTouched) {
                watchedConvertAudio.checked = convertAudioCheckbox.checked;
            }
            updateConversionLabels();
        });

        let currentTab = 'results';
        let downloadingIds = new Set();
        let lastResults = [];
        let lastCompletedSearchQuery = '';
        let currentSearchToken = 0;
        let _searchProgressHideTimer = null;
        let _searchProgressRowTimers = [];
        let currentArtworkUrl = null;
        let currentArtworkArtist = null;
        let currentArtworkTitle = null;
        let currentSearchLogToken = null;
        let currentSource = 'all'; // Always search all sources
        const expandedJobIds = new Set();
        const watchedRefreshPending = new Map();
        let activeTagEditor = null;

        // Source selector - restore saved preference and wire up clicks
        (function initSourceSelector() {
            const buttons = document.querySelectorAll('#sourceSelector .source-option');
            buttons.forEach(btn => {
                if (btn.dataset.source === currentSource) {
                    btn.classList.add('active');
                } else {
                    btn.classList.remove('active');
                }
                btn.addEventListener('click', () => {
                    buttons.forEach(b => b.classList.remove('active'));
                    btn.classList.add('active');
                    currentSource = btn.dataset.source;
                    localStorage.setItem(userStorageKey('searchSource'), currentSource);
                });
            });
        })();
        const MAX_QUEUE_SIZE = 100;
        const QUEUE_PAGE_SIZE = 10;
        // How many jobs the Queue tab pulls down in one go, searching or not.
        const QUEUE_SEARCH_LIMIT = 250;
        let queuePage = 1;
        let allQueueJobs = [];
        let queueHistorySortMode = 'recent';
        let queueSearchTerm = '';
        let queueSearchDebounce = null;
        // Whole-queue status counts from the server, so the live summary keeps
        // telling the truth while a search has the visible list down to one card.
        let queueTotals = null;
        let currentBulkImportId = null;
        let bulkImportPollInterval = null;
        let longformSplitPollInterval = null;
        let longformDetectedContext = null;
        let queuePollInterval = null;
        let queueElapsedInterval = null;
        let queueLoadInFlight = false;
        let queueLastUpdatedAt = 0;
        let queueSummarySignature = '';
        let watchedRefreshPollInterval = null;
        let watchedLoadInFlight = false;
        const tagEditorOverlay = document.getElementById('tagEditorOverlay');
        const tagEditorArtist = document.getElementById('tagEditorArtist');
        const tagEditorTitle = document.getElementById('tagEditorTitle');
        const tagEditorAlbum = document.getElementById('tagEditorAlbum');
        const tagEditorAlbumArtist = document.getElementById('tagEditorAlbumArtist');
        const tagEditorYear = document.getElementById('tagEditorYear');
        const tagEditorTrackNumber = document.getElementById('tagEditorTrackNumber');
        const tagEditorTrackTotal = document.getElementById('tagEditorTrackTotal');
        const tagEditorMeta = document.getElementById('tagEditorMeta');
        const tagEditorPreview = document.getElementById('tagEditorPreview');
        const tagEditorError = document.getElementById('tagEditorError');
        const tagEditorGuessBtn = document.getElementById('tagEditorGuessBtn');
        const tagEditorResetBtn = document.getElementById('tagEditorResetBtn');
        const tagEditorSaveBtn = document.getElementById('tagEditorSaveBtn');

        // Preview state
        const previewAudio = document.getElementById('previewAudio');
        let hoverTimeout = null;
        let currentPreviewKey = null;
        let previewRequestKey = null;
        let previewRequestSerial = 0;
        let previewAbortController = null;
        let previewCache = new Map(); // Short-lived cache of resolved preview URLs
        const MAX_PREVIEW_CACHE = 100;
        const PREVIEW_CACHE_TTL = 2 * 60 * 1000;
        const HOVER_DELAY = 2000; // 2 seconds before preview starts
        let _previewFadeInterval = null;
        const PREVIEW_FADE_DURATION = 5000; // ms to ramp from 0 to target volume

        const previewVolume = 0.8; // Fixed preview volume - fade-in handles the ramp

        // Desktop preview button: some folk would rather press a button than have
        // music start up because the mouse paused on its way to the biscuit tin.
        // Kept in localStorage rather than on the server, since which machine you
        // happen to be sat at is the entire point of the setting.
        function desktopPreviewButtonEnabled() {
            return localStorage.getItem(userStorageKey('desktopPreviewButton')) === '1';
        }

        function resultTooltip(previewable) {
            if (!previewable) return 'Click to download';
            return desktopPreviewButtonEnabled()
                ? 'Press Preview to listen, click to download'
                : 'Hover to preview, click to download';
        }

        function applyDesktopPreviewButton() {
            const on = desktopPreviewButtonEnabled();
            document.body.classList.toggle('desktop-preview-button', on);
            const toggle = document.getElementById('settingDesktopPreviewButton');
            if (toggle) toggle.checked = on;
            // Cards already on screen would otherwise carry on promising a hover
            // preview that is never coming, which is just rude.
            document.querySelectorAll('.result-item').forEach(item => {
                if (item.querySelector('.preview-btn')) item.dataset.tooltip = resultTooltip(true);
            });
        }

        // The button's own label doubles as its status light. A four second yt-dlp
        // resolve is otherwise indistinguishable from a button that does nothing,
        // and people press it again, which helps nobody.
        const PREVIEW_BTN_LABELS = {
            idle: 'Preview \u25B6',
            loading: 'Loading...',
            playing: 'Stop \u25A0',
        };

        function setPreviewBtnLabel(element, state) {
            const btn = element && element.querySelector && element.querySelector('.preview-btn');
            if (btn) btn.textContent = PREVIEW_BTN_LABELS[state];
        }

        function resetPreviewBtnLabels() {
            document.querySelectorAll('.preview-btn').forEach(btn => {
                btn.textContent = PREVIEW_BTN_LABELS.idle;
            });
        }

        function previewKey(videoId, result) {
            const source = (result && result.source) || 'youtube';
            const identity = (result && result.source_url) || videoId;
            return `${source}:${identity}`;
        }

        function previewIsActiveOrLoading(videoId, result) {
            const key = previewKey(videoId, result);
            return currentPreviewKey === key || previewRequestKey === key;
        }

        // Leaving a card cancels a hover-started preview. With the button on, the
        // preview was a deliberate press, so it carries on until told otherwise.
        function stopHoverPreview() {
            if (desktopPreviewButtonEnabled()) return;
            stopPreview();
        }

        // Preview functions
        function startHoverTimer(videoId, element, result) {
            clearHoverTimer();
            // With the button on, previews are strictly opt-in: a resting mouse is
            // no longer taken as consent.
            if (desktopPreviewButtonEnabled()) return;
            hoverTimeout = setTimeout(() => {
                startPreview(videoId, element, result);
            }, HOVER_DELAY);
        }

        function clearHoverTimer() {
            if (hoverTimeout) {
                clearTimeout(hoverTimeout);
                hoverTimeout = null;
            }
        }

        async function startPreview(videoId, element, result) {
            // Don't preview if already downloading
            if (downloadingIds.has(videoId)) return;

            const cacheKey = previewKey(videoId, result);
            // Upgrade previews can invoke this from both mouseenter and click.
            // One in-flight request for a result is plenty.
            if (previewRequestKey === cacheKey) return;

            // Stop any current preview
            stopPreview();
            const requestId = ++previewRequestSerial;
            previewRequestKey = cacheKey;
            previewAbortController = new AbortController();

            element.classList.add('loading-preview');
            setPreviewBtnLabel(element, 'loading');

            try {
                const cachedPreview = previewCache.get(cacheKey);
                let audioUrl = null;
                if (cachedPreview && Date.now() - cachedPreview.cachedAt < PREVIEW_CACHE_TTL) {
                    audioUrl = cachedPreview.url;
                } else if (cachedPreview) {
                    previewCache.delete(cacheKey);
                }

                if (!audioUrl) {
                    // Build preview URL with source params
                    const previewSource = (result && result.source) || 'youtube';
                    const params = new URLSearchParams({ source: previewSource });
                    if ((previewSource === 'soundcloud' || previewSource === 'zvu4no' || previewSource === 'freemp3cloud' || previewSource === 'monochrome') && result.source_url) {
                        params.set('url', result.source_url);
                    }
                    if (previewSource === 'monochrome' && result) {
                        params.set('artist', result.channel || result.artist || '');
                        params.set('title', result.title || '');
                    }
                    const response = await apiFetch(`/api/preview/${encodeURIComponent(videoId)}?${params}`, {
                        signal: previewAbortController.signal,
                    });
                    if (requestId !== previewRequestSerial) return;
                    if (!response.ok) throw new Error('Failed to get preview');

                    const data = await response.json();
                    if (requestId !== previewRequestSerial) return;
                    audioUrl = data.url;
                    previewCache.set(cacheKey, { url: audioUrl, cachedAt: Date.now() });
                    if (previewCache.size > MAX_PREVIEW_CACHE) {
                        const oldestKey = previewCache.keys().next().value;
                        previewCache.delete(oldestKey);
                    }
                }

                if (requestId !== previewRequestSerial) return;

                element.classList.remove('loading-preview');
                element.classList.add('previewing');
                setPreviewBtnLabel(element, 'playing');
                currentPreviewKey = cacheKey;
                previewRequestKey = null;

                previewAudio.onerror = () => {
                    previewCache.delete(cacheKey);
                    if (currentPreviewKey === cacheKey) stopPreview();
                };
                // A preview that runs to the end used to leave the card looking as
                // though it were still going. Now that the button says "Stop", that
                // particular fib is a bit too obvious to leave in.
                previewAudio.onended = () => {
                    if (currentPreviewKey === cacheKey) stopPreview();
                };
                previewAudio.src = audioUrl;
                previewAudio.volume = 0;
                previewAudio.play().catch(() => {
                    // Autoplay blocked or other error
                    previewCache.delete(cacheKey);
                    if (currentPreviewKey === cacheKey) stopPreview();
                });

                // Fade in to target volume over PREVIEW_FADE_DURATION
                if (_previewFadeInterval) clearInterval(_previewFadeInterval);
                const fadeSteps = 30;
                const fadeStepMs = PREVIEW_FADE_DURATION / fadeSteps;
                let step = 0;
                _previewFadeInterval = setInterval(() => {
                    step++;
                    previewAudio.volume = Math.min(previewVolume, (step / fadeSteps) * previewVolume);
                    if (step >= fadeSteps) clearInterval(_previewFadeInterval);
                }, fadeStepMs);


            } catch (error) {
                if (requestId !== previewRequestSerial) return;
                element.classList.remove('loading-preview');
                setPreviewBtnLabel(element, 'idle');
                // Silently fail - preview is a nice-to-have
            } finally {
                if (requestId === previewRequestSerial) {
                    previewRequestKey = null;
                    previewAbortController = null;
                }
            }
        }

        function stopPreview() {
            clearHoverTimer();
            previewRequestSerial++;
            if (previewAbortController) previewAbortController.abort();
            previewAbortController = null;
            previewRequestKey = null;
            if (_previewFadeInterval) { clearInterval(_previewFadeInterval); _previewFadeInterval = null; }
            previewAudio.onerror = null;
            previewAudio.onended = null;
            previewAudio.pause();
            previewAudio.src = '';
            currentPreviewKey = null;

            // Remove previewing class from all items
            document.querySelectorAll('.result-item.previewing').forEach(item => {
                item.classList.remove('previewing');
            });
            document.querySelectorAll('.result-item.loading-preview').forEach(item => {
                item.classList.remove('loading-preview');
            });
            resetPreviewBtnLabels();
        }

        // Search history management
        function getSearchHistory() {
            const history = localStorage.getItem(userStorageKey('searchHistory'));
            return history ? JSON.parse(history) : [];
        }

        function saveSearchHistory(query) {
            let history = getSearchHistory();
            // Remove duplicates
            history = history.filter(q => q !== query);
            // Add to front
            history.unshift(query);
            // Keep last 10
            history = history.slice(0, 10);
            localStorage.setItem(userStorageKey('searchHistory'), JSON.stringify(history));
        }

        function showSearchHistory() {
            const history = getSearchHistory();
            if (history.length === 0) {
                searchHistory.classList.remove('show');
                return;
            }

            searchHistory.innerHTML = history.map(q => `
                <div class="history-item" data-query="${escapeHtml(q)}">
                    ${escapeHtml(q)}
                </div>
            `).join('');

            searchHistory.querySelectorAll('.history-item').forEach(item => {
                item.addEventListener('click', () => {
                    setSearchValue(item.dataset.query);
                    searchHistory.classList.remove('show');
                    search();
                });
            });

            searchHistory.classList.add('show');
        }

        function hideSearchHistory() {
            setTimeout(() => {
                searchHistory.classList.remove('show');
            }, 200);
        }

        // Tab switching
        const allTabPanels = [resultsTab, bulkTabContainer, artistsTabContainer, watchedTabContainer, queueTabContainer, statsTabContainer, settingsTabContainer];
        tabs.forEach(tab => {
            tab.addEventListener('click', () => {
                tabs.forEach(t => t.classList.remove('active'));
                tab.classList.add('active');
                currentTab = tab.dataset.tab;

                // Remove visible from all panels, then re-add on the next frame so the
                // transition actually fires (display:none -> display:block needs a tick to paint)
                allTabPanels.forEach(p => p.classList.remove('tab-visible'));
                const panelMap = {
                    results: resultsTab,
                    bulk: bulkTabContainer,
                    artists: artistsTabContainer,
                    watched: watchedTabContainer,
                    queue: queueTabContainer,
                    stats: statsTabContainer,
                    settings: settingsTabContainer
                };
                const target = panelMap[currentTab];
                if (target) target.classList.add('tab-visible');

                // These controls sit above the tab panels in the DOM, so explicitly
                // scope them to Results instead of letting related-search chips (and
                // friends) wander into Artists, Queue, Settings, etc.
                [
                    document.getElementById('singleTrackSearch'),
                    document.getElementById('sourceHealthStrip'),
                    relatedSuggestions,
                    exploreBar,
                    document.getElementById('searchProgress'),
                    document.getElementById('destinationPickerRow')
                ].forEach(element => {
                    element?.classList.toggle('results-context-hidden', currentTab !== 'results');
                });

                // Stop preview when leaving results tab
                if (currentTab !== 'results') {
                    stopPreview();
                }
                // Same idea for the Find New Artists taster preview
                if (currentTab !== 'artists') {
                    _stopDiscoverPreview();
                }

                if (currentTab === 'artists') {
                    // Defer one tick so direct album links can populate their richer
                    // MBID-backed state first. A plain tab click remains pristine and
                    // inherits the artist from the last successful singles search.
                    setTimeout(primeAlbumSearchFromResults, 0);
                }

                if (currentTab === 'queue') {
                    stopQueuePolling();
                    loadJobs().finally(scheduleQueuePoll);
                    queueElapsedInterval = setInterval(updateLiveQueueTimes, 1000);
                    loadDownloadable();
                    loadTrash();
                } else {
                    stopQueuePolling();
                    stopLibraryPlayback();
                }

                if (currentTab === 'watched') {
                    loadWatchedPlaylists();
                    loadWatchedUpgrades();
                    loadAudioAudit();
                    populateSourceChips();
                } else if (watchedRefreshPollInterval) {
                    clearInterval(watchedRefreshPollInterval);
                    watchedRefreshPollInterval = null;
                }
                if (currentTab !== 'watched' && audioAuditPollTimer) {
                    clearTimeout(audioAuditPollTimer);
                    audioAuditPollTimer = null;
                }

                if (currentTab === 'artists') {
                    loadWatchedArtists();
                }

                if (currentTab === 'bulk') {
                    populateSourceChips();
                }

                if (currentTab === 'stats') {
                    loadStats();
                }

                if (currentTab === 'settings') {
                    loadSettings();
                    loadBlacklist();
                    if (isAdmin()) loadUsers();
                }

                // Show floating save bar only on settings tab
                const floatingBar = document.getElementById('settingsFloatingBar');
                if (floatingBar) floatingBar.style.display = currentTab === 'settings' ? 'flex' : 'none';
            });
        });

        // Search
        let lastSourceHealthToastAt = 0;
        let sourceHealthState = new Map();
        let sourceHealthPollTimer = null;

        function showUnavailableSourcesToast(sources) {
            if (!Array.isArray(sources) || sources.length === 0) return;
            const now = Date.now();
            if (now - lastSourceHealthToastAt < 30000) return;
            lastSourceHealthToastAt = now;
            const first = sources[0];
            const retryMs = first.retry_at ? Math.max(0, first.retry_at * 1000 - now) : 0;
            const retryText = retryMs ? `, retrying in ~${Math.max(1, Math.ceil(retryMs / 60000))} min` : '';
            const moreText = sources.length > 1 ? ` (+${sources.length - 1} more)` : '';
            showToast(`${first.label || first.id} unavailable${moreText}${retryText}`);
        }

        function sourceHealthTitle(source) {
            const checked = source.checked_at
                ? new Date(source.checked_at * 1000).toLocaleString()
                : 'not checked yet';
            if (source.healthy === null) return `${source.label}: checking`;
            if (source.available) return `${source.label}: available, checked ${checked}`;
            const reason = source.reason ? `, ${source.reason}` : '';
            const retry = source.retry_at
                ? `, retry after ${new Date(source.retry_at * 1000).toLocaleTimeString()}`
                : '';
            return `${source.label}: unavailable${reason}${retry}`;
        }

        // Short labels just for the Sources health strip - full names would wrap the
        // row. The tooltip (sourceHealthTitle) still carries the full name on hover.
        const SOURCE_HEALTH_SHORT_LABELS = {
            monochrome: 'MonoC',
            youtube: 'YT',
            soundcloud: 'SC',
            zvu4no: 'zvu4no',
            freemp3cloud: 'MP3C',
            soulseek: 'SLSK',
        };

        function renderSourceHealth(sources) {
            const strip = document.getElementById('sourceHealthStrip');
            if (!strip) return;
            const enabled = (sources || []).filter(source => source.enabled !== false);
            if (!enabled.length) {
                strip.style.display = 'none';
                strip.innerHTML = '';
                return;
            }
            const chips = enabled.map(source => {
                const stateClass = source.healthy === null
                    ? 'checking'
                    : (source.available ? 'healthy' : 'unavailable');
                const shortLabel = SOURCE_HEALTH_SHORT_LABELS[source.id] || source.label || source.id;
                return `<span class="source-health-chip ${stateClass}" title="${escapeAttr(sourceHealthTitle(source))}">
                    <span class="source-health-dot"></span>${escapeHtml(shortLabel)}
                </span>`;
            }).join('');
            const recheck = isAdmin()
                ? '<button type="button" class="source-health-recheck" id="sourceHealthRecheck">Re-check</button>'
                : '';
            strip.innerHTML = `<span class="source-health-label">Sources</span>${chips}${recheck}`;
            strip.style.display = 'flex';
            document.getElementById('sourceHealthRecheck')?.addEventListener('click', recheckSourceHealth);
        }

        async function loadSourceHealth(notifyTransitions = true) {
            try {
                const response = await apiFetch('/api/sources/health');
                if (!response.ok) return;
                const data = await response.json();
                const sources = Array.isArray(data.sources) ? data.sources : [];
                const newlyParked = notifyTransitions ? sources.filter(source => {
                    const previous = sourceHealthState.get(source.id);
                    return previous && previous.available !== false && source.available === false;
                }) : [];
                sourceHealthState = new Map(sources.map(source => [source.id, source]));
                renderSourceHealth(sources);
                showUnavailableSourcesToast(newlyParked);
            } catch {
                // Health is a useful hint, never a reason to break the search page.
            }
        }

        function startSourceHealthPolling() {
            if (sourceHealthPollTimer) clearInterval(sourceHealthPollTimer);
            loadSourceHealth(false);
            sourceHealthPollTimer = setInterval(() => loadSourceHealth(true), 30000);
        }

        async function recheckSourceHealth() {
            const button = document.getElementById('sourceHealthRecheck');
            if (button) {
                button.disabled = true;
                button.textContent = 'Checking…';
            }
            try {
                const response = await apiFetch('/api/sources/health/recheck', { method: 'POST' });
                if (!response.ok) throw new Error('Re-check failed');
                showToast('Source re-check started');
                setTimeout(() => loadSourceHealth(true), 1500);
                setTimeout(() => loadSourceHealth(true), 5000);
            } catch {
                showToast('Could not re-check sources', true);
            } finally {
                setTimeout(() => {
                    const current = document.getElementById('sourceHealthRecheck');
                    if (current) {
                        current.disabled = false;
                        current.textContent = 'Re-check';
                    }
                }, 1500);
            }
        }

        function clearDuplicateNotice() {
            const existing = document.getElementById('searchDuplicateNotice');
            if (existing) existing.remove();
        }

        function renderDuplicateNotice(notice) {
            clearDuplicateNotice();
            if (!notice || !notice.message) return;
            const sources = Array.isArray(notice.sources) ? notice.sources.map(s => s.source).filter(Boolean) : [];
            const sourceText = sources.length ? `Found via ${sources.map(escapeHtml).join(', ')}` : 'Already in your library';
            resultsTab.insertAdjacentHTML('afterbegin', `
                <div id="searchDuplicateNotice" class="search-duplicate-notice">
                    <i class="fa-solid fa-circle-info"></i>
                    <div>
                        <div class="search-duplicate-message">${escapeHtml(notice.message)}</div>
                        <div class="search-duplicate-source">${sourceText}</div>
                    </div>
                </div>
            `);
        }

        async function search() {
            if (searchBtn.disabled) return;
            const query = searchInput.value.trim();
            if (!query) return;

            // Links (playlists, albums, long videos worth splitting) belong in
            // Bulk Import, which already knows what to do with every URL shape
            // we support - send it there instead of searching Tracks for a URL.
            if (/^https?:\/\//i.test(query)) {
                searchInput.value = '';
                hideSearchHistory();
                document.querySelector('.tab[data-tab="bulk"]')?.click();
                spotifyUrlInput.value = query;
                await fetchSpotifyPlaylist();
                return;
            }

            const searchToken = ++currentSearchToken;
            currentSearchLogToken = null;
            lastCompletedSearchQuery = '';
            lastResults = [];

            // Save to history
            saveSearchHistory(query);
            hideSearchHistory();

            searchBtn.disabled = true;
            searchBtn.textContent = 'Searching…';
            resultsTab.innerHTML = `
                <div class="loading loading-with-copy" role="status">
                    <div class="spinner"></div>
                    <span>Warming up the search party…</span>
                </div>
            `;
            clearDuplicateNotice();
            relatedSuggestions.style.display = 'none';
            exploreBar.style.display = 'none';
            const _qualityRow = document.getElementById('qualityFilterRow');
            if (_qualityRow) _qualityRow.style.display = 'none';
            const _destRow1 = document.getElementById('destinationPickerRow');
            if (_destRow1) _destRow1.style.display = 'none';
            _exploreOriginalResults = null;
            _exploreResolvedResults = null;
            currentArtworkUrl = null;
            currentArtworkArtist = null;
            currentArtworkTitle = null;
            stopPreview(); // Stop any playing preview

            try {
                await runSearchStream(query, searchToken);
                if (searchToken === currentSearchToken) lastCompletedSearchQuery = query;
            } catch (error) {
                if (searchToken !== currentSearchToken) return;
                // Streaming hiccup: fall back to the plain blocking search so the
                // user never ends up worse off than before we had a stream.
                console.log('search stream failed, falling back to blocking:', error);
                hideSearchProgress();
                try {
                    await runSearchBlocking(query, searchToken);
                    if (searchToken === currentSearchToken) lastCompletedSearchQuery = query;
                } catch (err2) {
                    if (searchToken !== currentSearchToken) return;
                    resultsTab.innerHTML = `
                        <div class="empty-state">
                            <div class="empty-state-icon"><i class="fa-solid fa-circle-exclamation"></i></div>
                            <p>Search failed. Try again.</p>
                        </div>
                    `;
                    showToast('Search failed', true);
                }
            } finally {
                if (searchToken === currentSearchToken) {
                    searchBtn.disabled = false;
                    searchBtn.textContent = 'Search';
                }
            }
        }

        // Streaming search: consume the NDJSON event stream and render results
        // progressively as each source lands, ticking off the source status panel.
        async function runSearchStream(query, searchToken) {
            const accumulator = [];
            let albumSuggestion = null;
            let renderedOnce = false;

            initSearchProgress();

            const response = await apiFetch('/api/search/stream', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ query, limit: 15, source: currentSource })
            });
            if (!response.ok || !response.body) throw new Error('stream unavailable');

            // Artwork only needs the query, so kick it off straight away.
            const artworkParsed = parseArtistTitle(query);
            if (artworkParsed) fetchAndApplyArtwork(artworkParsed.artist, artworkParsed.title, searchToken);

            function handleEvent(ev) {
                if (searchToken !== currentSearchToken) return;
                switch (ev.type) {
                    case 'start':
                        renderSearchProgressRows(ev.sources || []);
                        break;
                    case 'source':
                        updateSourceStatus(ev.source, ev.status, ev.count);
                        if (ev.status === 'done' && ev.results && ev.results.length) {
                            accumulator.push(...ev.results);
                            accumulator.sort((a, b) => (b.relevance_score || 0) - (a.relevance_score || 0));
                            renderResults(accumulator);
                            renderedOnce = true;
                            // Re-render wipes thumbnails, so re-apply any fetched artwork.
                            if (currentArtworkUrl && currentArtworkArtist && currentArtworkTitle) {
                                applyArtworkToResults(currentArtworkArtist, currentArtworkTitle, currentArtworkUrl);
                            }
                        }
                        break;
                    case 'album_suggestion':
                        albumSuggestion = ev;
                        break;
                    case 'done':
                        currentSearchLogToken = ev.search_token || null;
                        showUnavailableSourcesToast(ev.unavailable_sources);
                        if (!renderedOnce) renderResults(accumulator); // shows the empty state
                        renderDuplicateNotice(ev.duplicate_notice);
                        showRelatedSuggestions(accumulator, albumSuggestion);
                        fetchAlbumArtwork(accumulator, searchToken);
                        finishSearchProgress();
                        break;
                    case 'error':
                        if (!renderedOnce) throw new Error(ev.detail || 'search failed');
                        finishSearchProgress();
                        break;
                }
            }

            const reader = response.body.getReader();
            const decoder = new TextDecoder();
            let buffer = '';
            while (true) {
                const { value, done } = await reader.read();
                if (done) break;
                if (searchToken !== currentSearchToken) { try { reader.cancel(); } catch (e) {} return; }
                buffer += decoder.decode(value, { stream: true });
                let nl;
                while ((nl = buffer.indexOf('\n')) >= 0) {
                    const line = buffer.slice(0, nl).trim();
                    buffer = buffer.slice(nl + 1);
                    if (!line) continue;
                    let ev;
                    try { ev = JSON.parse(line); } catch (e) { continue; }
                    handleEvent(ev);
                }
            }
            const tail = buffer.trim();
            if (tail) { try { handleEvent(JSON.parse(tail)); } catch (e) {} }
        }

        // Plain blocking search, kept as a fallback for when the stream falls over.
        async function runSearchBlocking(query, searchToken) {
            const response = await apiFetch('/api/search', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ query, limit: 15, source: currentSource })
            });
            if (!response.ok) throw new Error('Search failed');

            const data = await response.json();
            if (searchToken !== currentSearchToken) return;
            lastResults = data.results;
            currentSearchLogToken = data.search_token || null;
            showUnavailableSourcesToast(data.unavailable_sources);
            renderResults(data.results);
            renderDuplicateNotice(data.duplicate_notice);
            showRelatedSuggestions(data.results, data.album_suggestion);

            const artworkParsed = parseArtistTitle(query);
            if (artworkParsed) {
                fetchAndApplyArtwork(artworkParsed.artist, artworkParsed.title, searchToken);
            }
            fetchAlbumArtwork(data.results, searchToken);
        }

        // ----- Live search progress panel -----
        // Quip pools live in static/quips.js (global SEARCH_QUIPS), sectioned per
        // source plus the funny-failure sections. Each row fades away on its own a
        // moment after it reports in, so the panel empties itself as sources land.

        function _randomQuip(list, fallback) {
            if (!Array.isArray(list) || !list.length) return fallback;
            return list[Math.floor(Math.random() * list.length)];
        }

        function _searchingQuip(source) {
            const s = (typeof SEARCH_QUIPS !== 'undefined' && SEARCH_QUIPS.searching) || null;
            const pool = s && (s[source] || s._default);
            return _randomQuip(pool, 'Having a rummage');
        }

        function _failureQuip(section, fallback) {
            const pool = (typeof SEARCH_QUIPS !== 'undefined' && SEARCH_QUIPS[section]) || null;
            return _randomQuip(pool, fallback);
        }

        function _slowQuip(source) {
            const s = (typeof SEARCH_QUIPS !== 'undefined' && SEARCH_QUIPS.slow) || null;
            const pool = s && (s[source] || s._default);
            return _randomQuip(pool, 'Still going, hang on');
        }

        // One dig at MusicBrainz's expense per Albums-browser loading state. No
        // queue-style rotation here: a single lookup has no state changes to key
        // a swap off, so we just pick once and let it stand.
        function _musicbrainzQuip() {
            return _randomQuip(typeof MUSICBRAINZ_QUIPS !== 'undefined' ? MUSICBRAINZ_QUIPS : null, 'Loading');
        }

        // If a source is still searching long after its mates finished, swap its
        // line for a "taking a while" quip (Soulseek's retry is the usual culprit).
        function _startRowSlowTimer(source) {
            const t = setTimeout(() => {
                const panel = document.getElementById('searchProgress');
                if (!panel) return;
                const row = panel.querySelector(`.search-progress-row[data-source="${CSS.escape(source)}"]`);
                if (!row || row.dataset.status !== 'searching') return;
                const text = row.querySelector('.search-progress-status');
                if (text) text.textContent = _slowQuip(source) + '…';
            }, 9000);
            _searchProgressRowTimers.push(t);
        }

        function _clearProgressRowTimers() {
            _searchProgressRowTimers.forEach(t => clearTimeout(t));
            _searchProgressRowTimers = [];
        }

        function initSearchProgress() {
            const panel = document.getElementById('searchProgress');
            if (!panel) return;
            if (_searchProgressHideTimer) { clearTimeout(_searchProgressHideTimer); _searchProgressHideTimer = null; }
            _clearProgressRowTimers();
            panel.classList.remove('search-progress-fading');
            panel.innerHTML = '';
            panel.style.display = '';
        }

        function _progressRowHtml(src) {
            return `
                <div class="search-progress-row" data-source="${escapeHtml(src)}" data-status="searching">
                    <span class="search-progress-icon"><span class="watched-refresh-spinner"></span></span>
                    <span class="search-progress-name">${escapeHtml(getSourceLabel(src))}</span>
                    <span class="search-progress-status">${escapeHtml(_searchingQuip(src))}&hellip;</span>
                </div>
            `;
        }

        function renderSearchProgressRows(sources) {
            const panel = document.getElementById('searchProgress');
            if (!panel) return;
            panel.innerHTML = sources.map(_progressRowHtml).join('');
            panel.style.display = sources.length ? '' : 'none';
            sources.forEach(_startRowSlowTimer);
        }

        function updateSourceStatus(source, status, count) {
            const panel = document.getElementById('searchProgress');
            if (!panel) return;
            let row = panel.querySelector(`.search-progress-row[data-source="${CSS.escape(source)}"]`);
            if (!row) {
                // A status turned up for a source we never listed (e.g. a parked
                // source skipped before its row existed). Make one so it's still seen.
                panel.insertAdjacentHTML('beforeend', _progressRowHtml(source));
                panel.style.display = '';
                row = panel.querySelector(`.search-progress-row[data-source="${CSS.escape(source)}"]`);
                if (!row) return;
            }
            row.dataset.status = status;
            const icon = row.querySelector('.search-progress-icon');
            const text = row.querySelector('.search-progress-status');
            if (status === 'done') {
                icon.innerHTML = '<i class="fa-solid fa-circle-check"></i>';
                if (count) {
                    text.textContent = `${count} result${count === 1 ? '' : 's'}`;
                } else {
                    // Soulseek gets its own "soulless" line; everyone else the generic empty pool.
                    text.textContent = source === 'soulseek'
                        ? _failureQuip('soulless', 'no souls found')
                        : _failureQuip('empty', 'nothing here');
                }
            } else if (status === 'timeout') {
                icon.innerHTML = '<i class="fa-solid fa-hourglass-end"></i>';
                text.innerHTML = `${escapeHtml(_failureQuip('timeout', 'took too long, moved on'))}<button type="button" class="search-source-retry">Retry this source</button>`;
                text.querySelector('.search-source-retry')?.addEventListener('click', () => retrySearchSource(source, row));
            } else if (status === 'skipped') {
                icon.innerHTML = '<i class="fa-solid fa-circle-minus"></i>';
                text.textContent = _failureQuip('offline', 'offline, skipped');
            } else if (status === 'error') {
                icon.innerHTML = '<i class="fa-solid fa-triangle-exclamation"></i>';
                text.innerHTML = `${escapeHtml(_failureQuip('error', 'had a wobble'))}<button type="button" class="search-source-retry">Retry this source</button>`;
                text.querySelector('.search-source-retry')?.addEventListener('click', () => retrySearchSource(source, row));
            }
            if (status !== 'timeout' && status !== 'error') _fadeOutRow(row);
        }

        async function retrySearchSource(source, row) {
            const query = searchInput.value.trim();
            if (!query || !row) return;
            const searchToken = currentSearchToken;
            row.classList.remove('search-progress-row-fading');
            row.dataset.status = 'searching';
            const icon = row.querySelector('.search-progress-icon');
            const text = row.querySelector('.search-progress-status');
            if (icon) icon.innerHTML = '<span class="watched-refresh-spinner"></span>';
            if (text) text.textContent = `${_searchingQuip(source)}…`;
            try {
                const response = await apiFetch('/api/search', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ query, limit: 15, source })
                });
                if (!response.ok) throw new Error('retry failed');
                const data = await response.json();
                if (searchToken !== currentSearchToken) return;
                const merged = [...lastResults];
                const seen = new Set(merged.map(result => `${result.source}:${result.video_id}`));
                for (const result of data.results || []) {
                    const key = `${result.source}:${result.video_id}`;
                    if (!seen.has(key)) {
                        seen.add(key);
                        merged.push(result);
                    }
                }
                merged.sort((a, b) => (b.relevance_score || 0) - (a.relevance_score || 0));
                renderResults(merged);
                updateSourceStatus(source, 'done', (data.results || []).length);
                if (currentArtworkUrl && currentArtworkArtist && currentArtworkTitle) {
                    applyArtworkToResults(currentArtworkArtist, currentArtworkTitle, currentArtworkUrl);
                }
            } catch {
                if (searchToken === currentSearchToken) updateSourceStatus(source, 'error');
            }
        }

        function _fadeOutRow(row) {
            // Hold the final status on screen for a good while so it's easy to read
            // (success or failure alike), then drift it away on a gentle fade.
            const hold = setTimeout(() => {
                row.classList.add('search-progress-row-fading');
                const drop = setTimeout(() => {
                    row.remove();
                    const panel = document.getElementById('searchProgress');
                    if (panel && !panel.querySelector('.search-progress-row')) hideSearchProgress();
                }, 1300);
                _searchProgressRowTimers.push(drop);
            }, 3600);
            _searchProgressRowTimers.push(hold);
        }

        function finishSearchProgress() {
            // Rows fade out individually as they finish, so this is just a safety net:
            // if anything's still hanging about after a while, clear the panel.
            const panel = document.getElementById('searchProgress');
            if (!panel) return;
            if (_searchProgressHideTimer) clearTimeout(_searchProgressHideTimer);
            _searchProgressHideTimer = setTimeout(() => {
                if (!panel.querySelector('.search-progress-row')) hideSearchProgress();
            }, 8000);
        }

        function hideSearchProgress() {
            const panel = document.getElementById('searchProgress');
            if (!panel) return;
            if (_searchProgressHideTimer) { clearTimeout(_searchProgressHideTimer); _searchProgressHideTimer = null; }
            _clearProgressRowTimers();
            panel.classList.remove('search-progress-fading');
            panel.style.display = 'none';
            panel.innerHTML = '';
        }

        function parseArtistTitle(query) {
            const match = query.match(/^(.+?)\s+-\s+(.+)$/);
            if (!match) return null;
            return { artist: match[1].trim(), title: match[2].trim() };
        }

        async function fetchAndApplyArtwork(artist, title, searchToken) {
            try {
                const resp = await apiFetch(
                    `/api/search/artwork?artist=${encodeURIComponent(artist)}&title=${encodeURIComponent(title)}`
                );
                if (!resp.ok || searchToken !== currentSearchToken) return;
                const data = await resp.json();
                if (!data.url) return;
                currentArtworkUrl = data.url;
                currentArtworkArtist = artist;
                currentArtworkTitle = title;
                applyArtworkToResults(artist, title, data.url);
            } catch (e) {
                // Artwork is purely cosmetic, never fail the search over it
            }
        }

        function applyArtworkToResults(queryArtist, queryTitle, artworkUrl) {
            const norm = s => s.toLowerCase().replace(/[^a-z0-9\s]/g, '').replace(/\s+/g, ' ').trim();
            const normArtist = norm(queryArtist);
            const normTitle = norm(queryTitle);

            resultsTab.querySelectorAll('.result-item').forEach((item, index) => {
                const result = lastResults[index];
                if (!result) return;

                // Only fill in blank placeholders, leave source thumbnails alone
                const thumb = item.querySelector('.result-thumb');
                if (!thumb || thumb.tagName === 'IMG') return;

                const resultArtist = norm(result.artist || result.channel || '');
                const resultTitle = norm(result.title || '');
                const artistMatch = resultArtist.includes(normArtist) || normArtist.includes(resultArtist);
                const titleMatch = resultTitle.includes(normTitle) || normTitle.includes(resultTitle);

                if (artistMatch && titleMatch) {
                    // Marked as the query-level guess so a per-album cover, which
                    // knows more, is allowed to overrule it later.
                    thumb.replaceWith(_artworkImage(artworkUrl, 'is-query-art'));
                }
            });
        }

        function _artworkImage(url, extraClass) {
            const img = document.createElement('img');
            img.className = `result-thumb has-artwork${extraClass ? ' ' + extraClass : ''}`;
            img.src = url;
            img.alt = '';
            img.loading = 'lazy';
            return img;
        }

        // ----- Per-album cover art -----
        // slskd has no idea what a cover is, but the shared file's own path usually
        // names the album, so one lookup covers every result from the same record.
        // Cached for the session: search the same album twice and it costs nothing.
        const ALBUM_ARTWORK_MAX_LOOKUPS = 8;
        const albumArtworkCache = new Map();

        function albumArtworkKey(artist, album) {
            return `${(artist || '').toLowerCase().trim()}|${(album || '').toLowerCase().trim()}`;
        }

        function applyCachedAlbumArtwork() {
            resultsTab.querySelectorAll('.result-item').forEach(item => {
                const result = lastResults[parseInt(item.dataset.index)];
                if (!result || !result.artist || !result.album) return;

                const url = albumArtworkCache.get(albumArtworkKey(result.artist, result.album));
                if (!url) return;

                const thumb = item.querySelector('.result-thumb');
                if (!thumb) return;
                // Fill blanks, and overrule the query-level guess, but never touch a
                // thumbnail the source itself provided.
                if (thumb.tagName === 'IMG' && !thumb.classList.contains('is-query-art')) return;
                thumb.replaceWith(_artworkImage(url));
            });
        }

        async function fetchAlbumArtwork(results, searchToken) {
            const wanted = new Map();
            (results || []).forEach(r => {
                if (!r.artist || !r.album || r.thumbnail) return;
                const key = albumArtworkKey(r.artist, r.album);
                if (albumArtworkCache.has(key) || wanted.has(key)) return;
                wanted.set(key, { artist: r.artist, album: r.album });
            });
            if (!wanted.size) return;

            const lookups = [...wanted.entries()].slice(0, ALBUM_ARTWORK_MAX_LOOKUPS);
            await Promise.all(lookups.map(async ([key, { artist, album }]) => {
                try {
                    const resp = await apiFetch(
                        `/api/search/artwork?artist=${encodeURIComponent(artist)}&album=${encodeURIComponent(album)}`
                    );
                    if (!resp.ok) return;
                    const data = await resp.json();
                    if (data.url) albumArtworkCache.set(key, data.url);
                } catch (e) {
                    // Covers are decoration; a missing one never fails a search
                }
            }));

            if (searchToken === currentSearchToken) applyCachedAlbumArtwork();
        }

        // ----- Album browser cover art (Albums tab / Bulk Import artist view) -----
        // Same cache and courtesy cap as the search-result covers above, just aimed
        // at a discography list instead of a page of search hits. A prolific artist
        // can easily clear ALBUM_ARTWORK_MAX_LOOKUPS, so this works through the list
        // in batches rather than giving up after the first one.
        function _albumListThumbImage(url) {
            const img = document.createElement('img');
            img.className = 'album-list-thumb has-artwork';
            img.src = url;
            img.alt = '';
            img.loading = 'lazy';
            return img;
        }

        async function fetchAlbumListArtwork(targets, artistName) {
            if (!artistName || !targets || !targets.length) return;

            const lookups = [];
            for (const t of targets) {
                if (!t.title) continue;
                const key = albumArtworkKey(artistName, t.title);
                const cached = albumArtworkCache.get(key);
                if (cached) {
                    if (t.thumbEl && t.thumbEl.isConnected) t.thumbEl.replaceWith(_albumListThumbImage(cached));
                    continue;
                }
                lookups.push({ thumbEl: t.thumbEl, title: t.title, key });
            }

            for (let i = 0; i < lookups.length; i += ALBUM_ARTWORK_MAX_LOOKUPS) {
                const batch = lookups.slice(i, i + ALBUM_ARTWORK_MAX_LOOKUPS);
                await Promise.all(batch.map(async ({ thumbEl, title, key }) => {
                    try {
                        const resp = await apiFetch(
                            `/api/search/artwork?artist=${encodeURIComponent(artistName)}&album=${encodeURIComponent(title)}`
                        );
                        if (!resp.ok) return;
                        const data = await resp.json();
                        if (!data.url) return;
                        albumArtworkCache.set(key, data.url);
                        // The row may have been rebuilt (new artist picked, list
                        // reloaded) while this was in flight; a detached element
                        // just quietly drops the swap.
                        if (thumbEl && thumbEl.isConnected) thumbEl.replaceWith(_albumListThumbImage(data.url));
                    } catch (e) {
                        // Covers are decoration; a missing one never blocks the album list
                    }
                }));
            }
        }

        // Show related search suggestions based on artists, plus album suggestion if available
        function showRelatedSuggestions(results, albumSuggestion) {
            if (!results || results.length === 0) return;

            // Extract unique artists, falling back to the uploader only where there is
            // no artist at all. Soulseek usernames make for hopeless search suggestions:
            // nobody has ever wanted to see more tracks by "2jqll9htuy62asp1wu".
            const artists = [...new Set(
                results
                    .map(r => (r.source === 'soulseek' ? r.artist : (r.artist || r.channel)))
                    .map(name => (name || '').trim())
                    .filter(Boolean)
            )].slice(0, 5);

            if (artists.length === 0 && !albumSuggestion) return;

            // Album suggestion section (shown first when the backend found a matching album)
            let albumHtml = '';
            if (albumSuggestion) {
                albumHtml = `
                    <div class="related-title">Artist and Album</div>
                    <div class="suggestion-chips">
                        <button class="suggestion-chip album-suggestion-chip"
                            data-artist="${escapeAttr(albumSuggestion.artist_name)}"
                            data-artist-mbid="${escapeAttr(albumSuggestion.artist_mbid)}"
                            data-album="${escapeAttr(albumSuggestion.album_title)}"
                            data-release-mbid="${escapeAttr(albumSuggestion.release_mbid)}">
                            <i class="fa-solid fa-compact-disc"></i> ${escapeHtml(albumSuggestion.artist_name)} \u2013 ${escapeHtml(albumSuggestion.album_title)}
                        </button>
                    </div>
                `;
            }

            let artistHtml = '';
            if (artists.length > 0) {
                artistHtml = `
                    <div class="related-title">Related searches</div>
                    <div class="suggestion-chips">
                        ${artists.map(artist => `
                            <button class="suggestion-chip" data-query="${escapeHtml(artist)}">
                                ${escapeHtml(artist)}
                            </button>
                        `).join('')}
                    </div>
                `;
            }

            relatedSuggestions.innerHTML = `<div class="related-suggestions">${albumHtml}${artistHtml}</div>`;

            // Wire artist chip click handlers
            relatedSuggestions.querySelectorAll('.suggestion-chip:not(.album-suggestion-chip)').forEach(chip => {
                chip.addEventListener('click', () => {
                    setSearchValue(chip.dataset.query);
                    search();
                });
            });

            // Wire album suggestion chip click handler
            const albumChip = relatedSuggestions.querySelector('.album-suggestion-chip');
            if (albumChip) {
                albumChip.addEventListener('click', () => {
                    openAlbumFromSearch({
                        artistName: albumChip.dataset.artist,
                        albumTitle: albumChip.dataset.album,
                        artistMbid: albumChip.dataset.artistMbid,
                        releaseMbid: albumChip.dataset.releaseMbid,
                    });
                });
            }

            relatedSuggestions.style.display = 'block';
        }

        // -----------------------------------------------------------------------
        // Explore similar artists via ListenBrainz
        // -----------------------------------------------------------------------

        let _exploreOriginalResults = null;  // stash so Back restores them
        let _exploreResolvedResults = null;  // the actual search results from the explore run

        const exploreBar = document.getElementById('exploreBar');

        async function exploreSimilar(artist) {
            if (!artist) return;
            stopPreview();

            // The search that put these results on screen may still be streaming
            // (Soulseek takes its sweet time), and its later source events would
            // cheerfully re-render the original query straight over the top of
            // our similar-artist results. Bumping the token retires it exactly
            // as starting a new search would: the stream, its per-source retries
            // and the artwork fetches all check this before touching the DOM.
            const runToken = ++currentSearchToken;
            hideSearchProgress();

            // Stash current results so "Back" can restore them
            _exploreOriginalResults = lastResults ? [...lastResults] : [];

            // Show the explore bar with a loading state
            _showExploreBar(artist, null);

            // Show a spinner in results while we fetch
            resultsTab.innerHTML = '<div class="loading"><div class="spinner"></div></div>';
            relatedSuggestions.style.display = 'none';

            let tracks;
            const MAX_RETRIES = 3;
            const RETRY_DELAY_MS = 1500;
            let lastError;
            for (let attempt = 0; attempt < MAX_RETRIES; attempt++) {
                if (attempt > 0) await new Promise(r => setTimeout(r, RETRY_DELAY_MS));
                try {
                    const resp = await apiFetch('/api/explore/similar', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ artist, mode: 'easy', limit: 25 })
                    });
                    if (!resp.ok) throw new Error('Could not reach similar-artists service');
                    const data = await resp.json();
                    tracks = data.artists || [];
                    lastError = null;
                    break;
                } catch (e) {
                    lastError = e;
                }
            }
            if (runToken !== currentSearchToken) return;  // user moved on
            if (lastError) {
                resultsTab.innerHTML = `<div class="empty-state"><p>Could not load similar artists: ${escapeHtml(lastError.message)}</p></div>`;
                return;
            }

            if (!tracks.length) {
                resultsTab.innerHTML = `<div class="empty-state"><p>No similar artists found for "${escapeHtml(artist)}"</p></div>`;
                return;
            }

            // Update bar now we know the count
            _showExploreBar(artist, tracks);

            // Search each similar artist in parallel (batches of 5) and render as results arrive
            resultsTab.innerHTML = '';
            const exploreResults = [];

            async function searchOne(track) {
                // track is {artist} - search by artist name to get their top result
                const query = track.artist;
                try {
                    const resp = await apiFetch('/api/search', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ query, limit: 1, source: currentSource })
                    });
                    if (!resp.ok) return null;
                    const data = await resp.json();
                    return data.results && data.results[0] ? data.results[0] : null;
                } catch { return null; }
            }

            // Batch into groups of 5 to avoid hammering the backend
            for (let i = 0; i < tracks.length; i += 5) {
                const batch = tracks.slice(i, i + 5);
                const batchResults = await Promise.all(batch.map(searchOne));
                if (runToken !== currentSearchToken) return;  // a new search (or explore) owns the pane now
                batchResults.forEach(r => {
                    if (r) {
                        exploreResults.push(r);
                        // Render incrementally - append each card as it arrives
                        const tmp = document.createElement('div');
                        tmp.innerHTML = _renderOneResult(r, exploreResults.length - 1);
                        const item = tmp.firstElementChild;
                        resultsTab.appendChild(item);
                        _attachResultHandlers(item, r, exploreResults.length - 1);
                    }
                });
            }

            // Update lastResults so individual downloads work, and stash for Download All
            lastResults = exploreResults;
            _exploreResolvedResults = exploreResults;
            applyQualityFilter();

            // All batches done - enable the Download All button now it has real data
            const dlBtn = document.getElementById('exploreDownloadAllBtn');
            if (dlBtn) { dlBtn.disabled = false; dlBtn.textContent = 'Download All'; }

            if (!exploreResults.length) {
                resultsTab.innerHTML = `<div class="empty-state"><p>Couldn't find playable tracks for similar artists</p></div>`;
            }
        }

        function _showExploreBar(artist, tracks) {
            const trackCount = tracks ? tracks.length : null;
            const countText = trackCount ? ` (${trackCount} similar artists)` : '';
            exploreBar.innerHTML = `
                <div class="explore-bar">
                    <span class="explore-bar-label">~ Similar to <strong>${escapeHtml(artist)}</strong>${escapeHtml(countText)}</span>
                    <div class="explore-bar-actions">
                        ${tracks ? `
                            <label class="explore-playlist-label">
                                <input type="checkbox" id="explorePlaylistCheckbox" checked>
                                <span>Save as playlist</span>
                            </label>
                            <button class="explore-download-all-btn" id="exploreDownloadAllBtn" disabled>Loading...</button>
                        ` : ''}
                        <button class="explore-back-btn" id="exploreBackBtn">&#x2715; Back</button>
                    </div>
                </div>
            `;
            exploreBar.style.display = 'block';

            document.getElementById('exploreBackBtn').addEventListener('click', () => {
                exploreBar.style.display = 'none';
                stopPreview();
                if (_exploreOriginalResults) {
                    renderResults(_exploreOriginalResults);
                    showRelatedSuggestions(_exploreOriginalResults);
                }
                _exploreOriginalResults = null;
                _exploreResolvedResults = null;
            });

            const dlAllBtn = document.getElementById('exploreDownloadAllBtn');
            if (dlAllBtn && tracks) {
                dlAllBtn.addEventListener('click', () => _exploreDownloadAll(artist, tracks));
            }
        }

        async function _exploreDownloadAll(artist, tracks) {
            const dlAllBtn = document.getElementById('exploreDownloadAllBtn');
            if (dlAllBtn) { dlAllBtn.disabled = true; dlAllBtn.textContent = 'Queuing...'; }

            // Use the dedicated explore results (not lastResults, which may still hold the original search).
            const resolvedResults = _exploreResolvedResults && _exploreResolvedResults.length ? _exploreResolvedResults : null;
            let songs;
            if (resolvedResults) {
                songs = resolvedResults.map(r => {
                    const a = (r.artist || r.channel || '').trim();
                    const t = (r.title || '').trim();
                    return a && t ? `${a} - ${t}` : (a || t);
                }).filter(Boolean).join('\n');
            } else {
                songs = tracks.map(t => `${t.artist} - top track`).join('\n');
            }
            const now = new Date();
            const dateSuffix = `${now.getFullYear()}-${String(now.getMonth()+1).padStart(2,'0')}-${String(now.getDate()).padStart(2,'0')}`;
            const playlistName = `Similar to ${artist} (${dateSuffix})`;
            const makePlaylist = document.getElementById('explorePlaylistCheckbox')?.checked ?? true;

            const requestBody = { songs, convert_audio: true };
            if (makePlaylist) {
                requestBody.create_playlist = true;
                requestBody.playlist_name = playlistName;
                requestBody.use_playlists_dir = true;
            }

            try {
                const resp = await apiFetch('/api/bulk-import-async', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(requestBody)
                });
                if (!resp.ok) throw new Error('Bulk import failed');
                if (dlAllBtn) { dlAllBtn.textContent = 'Queued!'; }
                // Switch to the Queue tab so they can watch progress
                setTimeout(() => {
                    document.querySelector('[data-tab="queue"]')?.click();
                }, 800);
            } catch (e) {
                if (dlAllBtn) { dlAllBtn.disabled = false; dlAllBtn.textContent = 'Download All'; }
                showToast(`Could not queue downloads: ${e.message}`, true);
            }
        }

        // -----------------------------------------------------------------------
        // Reusable pill / segmented control
        //
        // A .pill-group holds any number of .pill buttons plus one .pill-indicator
        // span. Clicking a pill toggles .active and slides the indicator underneath
        // it (measured in pixels, so it copes fine with pills of different widths,
        // narrow screens, whatever). Fires a "pillchange" CustomEvent on the group
        // with { value } set from the clicked pill's data-value, so callers don't
        // need to know or care how the control itself works.
        // -----------------------------------------------------------------------

        function initPillGroup(group) {
            if (!group || group.dataset.pillInit) return;
            group.dataset.pillInit = '1';

            const pills = Array.from(group.querySelectorAll('.pill'));
            const indicator = group.querySelector('.pill-indicator');

            function moveIndicator() {
                const active = group.querySelector('.pill.active');
                if (!active || !indicator) return;
                indicator.style.width = `${active.offsetWidth}px`;
                indicator.style.transform = `translateX(${active.offsetLeft}px)`;
            }

            pills.forEach(pill => {
                pill.addEventListener('click', () => {
                    if (pill.classList.contains('active')) return;
                    pills.forEach(p => p.classList.remove('active'));
                    pill.classList.add('active');
                    moveIndicator();
                    group.dispatchEvent(new CustomEvent('pillchange', { detail: { value: pill.dataset.value } }));
                });
            });

            // Pills can reflow at narrow widths, so keep the indicator honest.
            window.addEventListener('resize', moveIndicator);
            // Defer so layout has actually settled before we measure it.
            requestAnimationFrame(moveIndicator);
        }

        // -----------------------------------------------------------------------
        // Find New Artists (Artists tab -> "Find New Artists" pill)
        //
        // Three ways in: the auto-generated rows from /api/discover/rows (seeded
        // from Navidrome history, library contents, or past downloads, in that
        // order of preference), a manual "recommend based on" box that hits the
        // existing /api/explore/similar, and the rabbit hole - click an artist's
        // name and their own similar artists land in a new row underneath. Falling
        // down a Discogs hole at 1am, but on purpose and with a way back out.
        // -----------------------------------------------------------------------

        let _discoverLoaded = false;       // have we ever fetched /api/discover/rows
        let _discoverAudio = null;         // the one taster <audio> allowed to play at a time
        let _discoverPlayingCard = null;   // card element currently sounding off
        let _discoverBreadcrumb = [];      // trail of artist names for the rabbit hole

        function _stopDiscoverPreview() {
            if (_discoverAudio) {
                _discoverAudio.pause();
                _discoverAudio.src = '';
                _discoverAudio = null;
            }
            if (_discoverPlayingCard) {
                _discoverPlayingCard.classList.remove('discover-card-playing');
                _discoverPlayingCard = null;
            }
        }

        function setArtistsMode(mode) {
            const followedPanel = document.getElementById('followedArtistsPanel');
            const discoverPanel = document.getElementById('findNewArtistsPanel');
            const refreshBtn = document.getElementById('refreshAllArtistsBtn');
            if (!followedPanel || !discoverPanel) return;
            const isDiscover = mode === 'discover';
            followedPanel.style.display = isDiscover ? 'none' : '';
            discoverPanel.style.display = isDiscover ? '' : 'none';
            if (refreshBtn) refreshBtn.style.display = isDiscover ? 'none' : '';
            // The blurb above the pills describes whichever half you are looking
            // at; leaving the Followed wording up while showing discoveries just
            // reads as the page talking about something else entirely.
            const intro = document.getElementById('artistsIntroText');
            if (intro) {
                intro.textContent = isDiscover
                    ? "Artists you have not got yet, worked out from what you actually listen to. Have a listen, grab the good ones, and follow the trail as far as it goes."
                    : "Find an artist, then pick what you want from them: their singles as they land, their albums, or just one album right now.";
            }
            if (isDiscover) {
                initDiscoverPanel();
            } else {
                _stopDiscoverPreview();
            }
        }

        function initDiscoverPanel() {
            if (_discoverLoaded) return;
            _discoverLoaded = true;
            loadDiscoverRows();
        }

        async function loadDiscoverRows() {
            const container = document.getElementById('discoverRowsContainer');
            if (!container) return;
            container.innerHTML = '<div class="loading"><div class="spinner"></div></div>';
            try {
                const resp = await apiFetch('/api/discover/rows?limit=5&per_row=10');
                if (!resp.ok) throw new Error('Could not reach the discovery service');
                const data = await resp.json();
                const rows = data.rows || [];
                if (data.signal === 'none' || !rows.length) {
                    container.innerHTML = `
                        <div class="empty-state">
                            <div class="empty-state-icon">🔇</div>
                            <p>Nothing to go on yet, you've been suspiciously well-behaved.</p>
                            <small>Listen to something in Navidrome, or download a track or two, and we'll start digging up lookalikes for you.</small>
                        </div>`;
                    return;
                }
                container.innerHTML = '';
                rows.forEach(row => {
                    const rowEl = discoverCreateRow(row.reason || `Because you listen to ${row.seed}`, { seed: row.seed });
                    container.appendChild(rowEl);
                    discoverFillRow(rowEl, row.artists || []);
                });
            } catch (e) {
                container.innerHTML = `
                    <div class="empty-state">
                        <div class="empty-state-icon">🕳️</div>
                        <p>Could not load recommendations: ${escapeHtml(e.message)}</p>
                        <button type="button" class="btn btn-primary" id="discoverRetryBtn">Try again</button>
                    </div>`;
                document.getElementById('discoverRetryBtn')?.addEventListener('click', () => {
                    _discoverLoaded = false;
                    initDiscoverPanel();
                });
            }
        }

        // Brings a newly-added row into view and says "over here" about it.
        //
        // scrollIntoView was doing two unhelpful things at once: putting the row
        // flush against the top of the window, which shoves the box you just
        // typed into off the screen, and moving so fast that the arrival was over
        // before the eye caught it. This lands the row a little way down instead,
        // takes its time getting there, and leaves the accent ring on it for a
        // moment so there is no doubt about which row is the new one.
        function discoverRevealRow(row, { offset = 110, duration = 850 } = {}) {
            if (!row) return;

            // Flag it first: even if the scroll is a no-op because the row is
            // already on screen, the highlight still has to happen.
            row.classList.add('discover-row-arrived');
            setTimeout(() => row.classList.remove('discover-row-arrived'), 2200);

            // Whichever ancestor actually scrolls, or the page if none does.
            let box = row.parentElement;
            while (box && box !== document.body) {
                const style = getComputedStyle(box);
                if (/(auto|scroll)/.test(style.overflowY) && box.scrollHeight > box.clientHeight) break;
                box = box.parentElement;
            }
            if (box === document.body) box = null;

            const from = box ? box.scrollTop : window.scrollY;
            const easy = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
            const frame = box ? box.getBoundingClientRect().top : 0;
            const to = Math.max(0, from + row.getBoundingClientRect().top - frame - offset);
            if (Math.abs(to - from) < 4) return;

            if (easy) {          // asked not to be swooshed about
                if (box) box.scrollTop = to; else window.scrollTo(0, to);
                return;
            }

            const start = performance.now();
            let lastSet = from;
            function step(now) {
                const here = box ? box.scrollTop : window.scrollY;
                // Somebody grabbed the wheel. Their scroll wins, always.
                if (Math.abs(here - lastSet) > 2) return;
                const t = Math.min(1, (now - start) / duration);
                // easeInOutCubic: unhurried at both ends, so the movement reads
                // as "look here" rather than as the page glitching.
                const eased = t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2;
                lastSet = from + (to - from) * eased;
                if (box) box.scrollTop = lastSet; else window.scrollTo(0, lastSet);
                if (t < 1) requestAnimationFrame(step);
            }
            requestAnimationFrame(step);
        }

        // Builds an empty row shell (heading + spinner) ready for discoverFillRow
        // to drop cards into once the artists for it are known.
        function discoverCreateRow(heading, opts = {}) {
            const row = document.createElement('div');
            row.className = 'discover-row';
            if (opts.rabbitHole) row.dataset.rabbitHole = '1';
            if (opts.seed) row.dataset.seed = opts.seed;
            row.innerHTML = `
                <div class="discover-row-header">
                    <h3 class="discover-row-heading">${escapeHtml(heading)}</h3>
                    <button type="button" class="btn btn-sm discover-row-grab-btn" disabled
                            title="Queue every artist in this row as one playlist">Grab the lot</button>
                </div>
                <div class="discover-row-body"><div class="loading"><div class="spinner"></div></div></div>
            `;
            row.querySelector('.discover-row-grab-btn')
               .addEventListener('click', () => discoverRowGrabAll(row));
            return row;
        }

        function discoverFillRow(row, artists) {
            const body = row.querySelector('.discover-row-body');
            const grabBtn = row.querySelector('.discover-row-grab-btn');
            if (!body) return;
            if (!artists || !artists.length) {
                body.innerHTML = '<p class="discover-row-empty">Nothing came back for this one, dead end in the tunnel.</p>';
                if (grabBtn) grabBtn.remove();  // nothing to grab, so stop advertising it
                return;
            }
            const scroll = document.createElement('div');
            scroll.className = 'discover-row-scroll';
            artists.forEach(a => scroll.appendChild(discoverBuildCard(a)));
            body.innerHTML = '';
            body.appendChild(scroll);
            if (grabBtn) grabBtn.disabled = false;
        }

        function discoverRowError(row, message, retryFn) {
            const body = row.querySelector('.discover-row-body');
            if (!body) return;
            body.innerHTML = `<p class="discover-row-empty">Could not load this one: ${escapeHtml(message)}. <button type="button" class="btn btn-ghost btn-sm discover-row-retry-btn">Try again</button></p>`;
            const retryBtn = body.querySelector('.discover-row-retry-btn');
            if (retryBtn && retryFn) retryBtn.addEventListener('click', retryFn);
        }

        // One artist card: art (with a graceful fallback), name (the rabbit hole),
        // a play button for the 30-second taster, and a download button.
        function discoverBuildCard(a) {
            const wrap = document.createElement('div');
            wrap.className = 'discover-card';
            wrap._artist = a;  // the row-wide "Grab the lot" needs these back
            wrap.innerHTML = `
                <div class="discover-card-art">
                    <span class="discover-card-art-fallback">🎵</span>
                    ${a.picture ? `<img class="discover-card-img" src="${escapeHtml(a.picture)}" alt="" loading="lazy">` : ''}
                </div>
                <button type="button" class="discover-card-name" title="See what's similar to ${escapeHtml(a.artist)}">${escapeHtml(a.artist)}</button>
                <span class="discover-owned-badge" hidden>Already got it</span>
                <div class="discover-card-title"></div>
                <div class="discover-card-actions">
                    <button type="button" class="btn btn-sm discover-play-btn" title="Preview">&#9654;</button>
                    <button type="button" class="btn btn-sm btn-primary discover-dl-btn" title="Download">Grab it</button>
                </div>
            `;

            const img = wrap.querySelector('.discover-card-img');
            // A dud picture URL should just fall back to the music-note glyph
            // underneath, not sit there as a broken-image icon looking sorry for itself.
            if (img) img.addEventListener('error', () => img.remove());

            const playBtn = wrap.querySelector('.discover-play-btn');
            const dlBtn = wrap.querySelector('.discover-dl-btn');
            const nameBtn = wrap.querySelector('.discover-card-name');

            playBtn.addEventListener('click', (e) => {
                e.stopPropagation();
                discoverPlayPreview(a.artist, a.deezer_id, wrap);
            });
            dlBtn.addEventListener('click', (e) => {
                e.stopPropagation();
                discoverDownload(a.artist, a.deezer_id, wrap);
            });
            nameBtn.addEventListener('click', () => discoverRabbitHole(a.artist));

            return wrap;
        }

        // Resolves (and caches on the card itself) the taster for one artist.
        // Returns null on a 404 (no preview available), which callers treat as
        // "nothing to play", not as an error worth shouting about.
        async function discoverResolveTaster(artist, deezerId, cardEl) {
            if (cardEl._taster) return cardEl._taster;
            if (cardEl._taster404) return null;

            // No Deezer id is not a dead end. Suggestions that came from
            // ListenBrainz rather than Deezer arrive without one, and those are
            // precisely the artists Deezer had never heard of, so refusing to
            // look them up would leave the most obscure finds unplayable. The
            // backend resolves by name when no id is given.
            let url = `/api/discover/taster?artist=${encodeURIComponent(artist)}`;
            if (deezerId) url += `&deezer_id=${encodeURIComponent(deezerId)}`;
            const resp = await apiFetch(url);
            if (resp.status === 404) {
                cardEl._taster404 = true;
                return null;
            }
            if (!resp.ok) throw new Error('Lookup failed');
            const data = await resp.json();
            cardEl._taster = data;

            const titleEl = cardEl.querySelector('.discover-card-title');
            if (titleEl && data.title) titleEl.textContent = data.title;
            const badge = cardEl.querySelector('.discover-owned-badge');
            if (badge && data.owned) badge.hidden = false;

            return data;
        }

        async function discoverPlayPreview(artist, deezerId, cardEl) {
            const playBtn = cardEl.querySelector('.discover-play-btn');
            if (!playBtn || playBtn.disabled) return;

            const wasPlayingThis = _discoverPlayingCard === cardEl;
            stopPreview();          // only one thing gets to make noise at a time
            _stopDiscoverPreview();
            if (wasPlayingThis) return; // clicking the active card's play button again just stops it

            cardEl.classList.add('discover-card-loading');
            try {
                const data = await discoverResolveTaster(artist, deezerId, cardEl);
                cardEl.classList.remove('discover-card-loading');
                if (!data || !data.preview_url) {
                    playBtn.disabled = true;
                    playBtn.title = 'No preview available for this one';
                    return;
                }
                _discoverAudio = new Audio(data.preview_url);
                _discoverAudio.volume = 0.9;
                _discoverAudio.addEventListener('ended', () => {
                    if (_discoverPlayingCard === cardEl) _stopDiscoverPreview();
                });
                await _discoverAudio.play().catch(() => {});
                cardEl.classList.add('discover-card-playing');
                _discoverPlayingCard = cardEl;
            } catch (e) {
                cardEl.classList.remove('discover-card-loading');
                showToast(`Preview failed: ${e.message}`, true);
            }
        }

        // Resolve the taster for its title (if we don't already have it), search
        // for "{artist} - {title}", and hand the top result to the same download
        // call the main search results use. Same trick as exploreSimilar's
        // searchOne, just for one card instead of a whole batch.
        async function discoverDownload(artist, deezerId, cardEl) {
            const dlBtn = cardEl.querySelector('.discover-dl-btn');
            if (!dlBtn || dlBtn.disabled) return;
            const originalLabel = dlBtn.textContent;
            dlBtn.disabled = true;
            dlBtn.textContent = 'Queuing...';

            try {
                let title = cardEl._taster?.title;
                if (!title) {
                    const resolved = await discoverResolveTaster(artist, deezerId, cardEl).catch(() => null);
                    title = resolved?.title;
                }
                // Searching for a specific track rather than just a name is the
                // whole reason the taster exists: it gives the matcher something
                // precise to score against, and is how a discovery comes back as
                // a lossless copy instead of whatever turned up first.
                const query = title ? `${artist} - ${title}` : artist;

                const searchResp = await apiFetch('/api/search', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ query, limit: 1, source: currentSource })
                });
                if (!searchResp.ok) throw new Error('Search failed');
                const searchData = await searchResp.json();
                const top = searchData.results && searchData.results[0];
                if (!top) throw new Error('Nothing found to download');

                const payload = {
                    video_id: top.video_id,
                    title: top.title,
                    convert_audio: convertAudioCheckbox.checked,
                    source: top.source || 'youtube'
                };
                if (top.artist || top.channel) payload.artist = top.artist || top.channel;
                if ((top.source === 'soundcloud' || top.source === 'zvu4no' || top.source === 'freemp3cloud' || top.source === 'monochrome') && top.source_url) {
                    payload.source_url = top.source_url;
                }
                if (top.source === 'soulseek') {
                    payload.slskd_username = top.slskd_username;
                    payload.slskd_filename = top.slskd_filename;
                    payload.slskd_size = top.slskd_size || top.size || null;
                }

                const downloadResp = await apiFetch('/api/download', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload)
                });
                if (!downloadResp.ok) throw new Error('Download failed');

                dlBtn.textContent = 'Queued';
                showToast(`Queued ${artist}${title ? ` - ${title}` : ''}`);
            } catch (e) {
                dlBtn.disabled = false;
                dlBtn.textContent = originalLabel;
                showToast(`Could not queue: ${e.message}`, true);
            }
        }

        // "Grab the lot": resolve a track for every artist in the row and hand
        // the lot to bulk import as one playlist. The tasters are what make this
        // worth doing; a list of bare artist names would just fetch whatever each
        // source happened to think was their finest hour.
        async function discoverRowGrabAll(row) {
            const btn = row.querySelector('.discover-row-grab-btn');
            const cards = Array.from(row.querySelectorAll('.discover-card'));
            if (!btn || !cards.length) return;
            const originalLabel = btn.textContent;
            btn.disabled = true;
            btn.textContent = 'Rounding up...';

            try {
                const lines = [];
                // Five at a time, same as the Similar search. Deezer is free and
                // we would quite like it to stay that way.
                for (let i = 0; i < cards.length; i += 5) {
                    const batch = cards.slice(i, i + 5);
                    const resolved = await Promise.all(batch.map(async card => {
                        const a = card._artist || {};
                        // Already queued from its own button, or the only track we
                        // could find is one you already own: either way, skip it.
                        if (card.querySelector('.discover-dl-btn')?.textContent === 'Queued') return null;
                        const data = await discoverResolveTaster(a.artist, a.deezer_id, card).catch(() => null);
                        if (!data || !data.title || data.owned) return null;
                        return `${a.artist} - ${data.title}`;
                    }));
                    resolved.forEach(line => { if (line) lines.push(line); });
                    btn.textContent = `Rounding up... ${lines.length}`;
                }

                if (!lines.length) {
                    btn.disabled = false;
                    btn.textContent = originalLabel;
                    showToast('Nothing left to grab in that row, you have the lot already', true);
                    return;
                }

                const seed = row.dataset.seed || 'your taste';
                const now = new Date();
                const dateSuffix = `${now.getFullYear()}-${String(now.getMonth()+1).padStart(2,'0')}-${String(now.getDate()).padStart(2,'0')}`;
                const playlistName = `Similar to ${seed} (${dateSuffix})`;

                const resp = await apiFetch('/api/bulk-import-async', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        songs: lines.join('\n'),
                        convert_audio: convertAudioCheckbox.checked,
                        create_playlist: true,
                        playlist_name: playlistName,
                        use_playlists_dir: true
                    })
                });
                if (!resp.ok) throw new Error('Bulk import failed');

                btn.textContent = `Queued ${lines.length}`;
                showToast(`Queued ${lines.length} track${lines.length === 1 ? '' : 's'} as "${playlistName}"`);
            } catch (e) {
                btn.disabled = false;
                btn.textContent = originalLabel;
                showToast(`Could not queue that row: ${e.message}`, true);
            }
        }

        // Fetches similar artists for one name and drops them into an existing
        // row shell. Used by the manual "recommend based on" box, the rabbit
        // hole, and both of their retry buttons.
        async function discoverLoadSimilarInto(row, artist) {
            try {
                const resp = await apiFetch('/api/explore/similar', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ artist, limit: 25 })
                });
                if (!resp.ok) throw new Error('Could not reach similar-artists service');
                const data = await resp.json();
                discoverFillRow(row, (data.artists || []).slice(0, 10));
            } catch (e) {
                discoverRowError(row, e.message, () => {
                    row.querySelector('.discover-row-body').innerHTML = '<div class="loading"><div class="spinner"></div></div>';
                    discoverLoadSimilarInto(row, artist);
                });
            }
        }

        async function discoverManualSearch() {
            const input = document.getElementById('discoverSimilarInput');
            const btn = document.getElementById('discoverSimilarBtn');
            const slot = document.getElementById('discoverManualRowSlot');
            const artist = input?.value.trim();
            if (!artist || !slot) return;

            btn.disabled = true;
            const row = discoverCreateRow(`Because you asked about ${artist}`, { seed: artist });
            slot.innerHTML = '';
            slot.appendChild(row);
            discoverRevealRow(row);
            try {
                await discoverLoadSimilarInto(row, artist);
            } finally {
                btn.disabled = false;
            }
        }

        // The fun bit: click an artist's name and their own similar artists
        // land in a new row at the bottom, with a breadcrumb trail so nobody
        // gets lost down there.
        function discoverRabbitHole(artist) {
            const container = document.getElementById('discoverRowsContainer');
            if (!container) return;

            _discoverBreadcrumb.push(artist);
            renderDiscoverBreadcrumb();

            const row = discoverCreateRow(`Because you liked the look of ${artist}`, { rabbitHole: true, seed: artist });
            container.appendChild(row);
            discoverRevealRow(row);
            discoverLoadSimilarInto(row, artist);
        }

        function renderDiscoverBreadcrumb() {
            const bc = document.getElementById('discoverBreadcrumb');
            if (!bc) return;
            if (!_discoverBreadcrumb.length) {
                bc.style.display = 'none';
                bc.innerHTML = '';
                return;
            }
            bc.style.display = 'flex';
            bc.innerHTML = `
                <span class="discover-breadcrumb-label">Down the rabbit hole:</span>
                ${_discoverBreadcrumb.map((name, i) => `<button type="button" class="discover-breadcrumb-item" data-depth="${i}">${escapeHtml(name)}</button>`).join('<span class="discover-breadcrumb-sep">/</span>')}
                <button type="button" class="btn btn-ghost btn-sm discover-breadcrumb-exit" id="discoverExitHoleBtn">Climb out</button>
            `;
            bc.querySelectorAll('.discover-breadcrumb-item').forEach(item => {
                item.addEventListener('click', () => discoverTrimTrail(parseInt(item.dataset.depth, 10)));
            });
            document.getElementById('discoverExitHoleBtn')?.addEventListener('click', () => discoverTrimTrail(-1));
        }

        // Trims the rabbit-hole rows (and breadcrumb) back to `depth`; -1 climbs
        // all the way out. Rabbit-hole rows are always appended in the same
        // order as the breadcrumb, so index-matching the two is safe.
        function discoverTrimTrail(depth) {
            const container = document.getElementById('discoverRowsContainer');
            if (!container) return;
            const rabbitRows = container.querySelectorAll('.discover-row[data-rabbit-hole="1"]');
            const keepCount = depth < 0 ? 0 : depth + 1;
            rabbitRows.forEach((row, i) => {
                if (i >= keepCount) row.remove();
            });
            _discoverBreadcrumb = _discoverBreadcrumb.slice(0, keepCount);
            renderDiscoverBreadcrumb();
            if (keepCount > 0) {
                discoverRevealRow(rabbitRows[keepCount - 1]);
            } else {
                document.getElementById('findNewArtistsPanel')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
            }
        }

        const artistsModePills = document.getElementById('artistsModePills');
        if (artistsModePills) {
            initPillGroup(artistsModePills);
            artistsModePills.addEventListener('pillchange', (e) => setArtistsMode(e.detail.value));
        }

        document.getElementById('discoverSimilarBtn')?.addEventListener('click', discoverManualSearch);
        document.getElementById('discoverSimilarInput')?.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') discoverManualSearch();
        });

        // ---------------------------------------------------------------
        // Minimum-quality filter
        //
        // Only Soulseek, Monochrome and FreeMp3Cloud declare a quality before
        // download; YouTube and SoundCloud say nothing at all, so they arrive as
        // tier 0 ("undeclared"). Asking for "Lossless only" and still being handed
        // a YouTube rip is not what anybody meant, so undeclared results are hidden
        // once a minimum is set. "Keep undeclared" brings them back for anyone who
        // wants the safety net. With the filter on "Any quality" nothing is hidden.
        //
        // Worth knowing: the list is ordered by relevance_score (title/artist match,
        // views, duration and source bonus), not by audio quality. YouTube
        // tends to top it because it has the exact title, which is precisely why an
        // undeclared result could otherwise sit above real lossless ones.
        //
        // Filtering hides cards rather than removing them, because every download
        // and preview handler is bound to the result's index in lastResults.
        // Re-indexing a filtered list is exactly the kind of clever that ends up
        // downloading the wrong song.
        // ---------------------------------------------------------------
        function applyQualityFilter() {
            const row = document.getElementById('qualityFilterRow');
            const select = document.getElementById('qualityFilterSelect');
            const keepUnknownEl = document.getElementById('qualityFilterKeepUnknown');
            const countEl = document.getElementById('qualityFilterCount');
            if (!row || !select) return;

            const items = resultsTab.querySelectorAll('.result-item');
            row.style.display = items.length ? '' : 'none';
            if (!items.length) return;

            const minTier = parseInt(select.value, 10) || 0;
            const keepUnknown = !!(keepUnknownEl && keepUnknownEl.checked);
            let hidden = 0;

            items.forEach(item => {
                const tier = parseInt(item.dataset.qualityTier, 10) || 0;
                const passes = minTier === 0
                    ? true
                    : (tier === 0 ? keepUnknown : tier >= minTier);
                item.style.display = passes ? '' : 'none';
                if (!passes) hidden++;
            });

            if (countEl) {
                countEl.textContent = hidden
                    ? `${items.length - hidden} of ${items.length} shown`
                    : '';
            }
        }

        // Sources with no preview endpoint (mirrors has_preview in search.py's SOURCE_REGISTRY).
        // Soulseek is the only holdout: there is no streamable URL until the file has
        // already been downloaded, at which point previewing it is rather beside the point.
        const NO_PREVIEW_SOURCES = new Set(['soulseek']);
        const sourceHasPreview = source => !NO_PREVIEW_SOURCES.has(source);

        // Render a single result card as an HTML string (mirrors the main renderResults template)
        function _renderOneResult(r, index) {
            const safeSource = escapeHtml(r.source || '');
            const safeVideoId = escapeHtml(r.video_id || '');
            const previewable = sourceHasPreview(r.source);
            return `
                <div class="result-item ${downloadingIds.has(r.video_id) ? 'downloading' : ''} ${r.source === 'soulseek' ? 'soulseek' : ''}"
                     data-video-id="${safeVideoId}"
                     data-quality-tier="${Number(r.quality_tier) || 0}"
                     data-index="${index}"
                     data-title="${escapeHtml(r.title)}"
                     data-tooltip="${resultTooltip(previewable)}">
                    ${previewable ? '<div class="preview-indicator">▶</div>' : ''}
                    ${r.thumbnail ? `<img class="result-thumb" src="${escapeHtml(r.thumbnail)}" alt="" loading="lazy">` : '<div class="result-thumb"></div>'}
                    <div class="result-info">
                        <div class="result-title">${escapeHtml(r.title)}</div>
                        <div class="result-meta">${formatResultMeta(r)}</div>
                        <div class="result-badges">
                            <span class="source-badge ${safeSource}">${getSourceBadge(r.source)}</span>
                            ${r.quality ? `<span class="quality-badge ${getQualityBadgeClass(r.quality)}">${formatQualityLabel(r.quality)}</span>` : ''}
                            ${r.duration ? `<span class="result-duration">${r.duration}</span>` : ''}
                        </div>
                    </div>
                    ${previewable ? `<div class="mobile-actions"><button class="preview-btn" data-video-id="${safeVideoId}" data-index="${index}" title="Preview">Preview &#9654;</button></div>` : ''}
                </div>
            `;
        }

        // Attach download/preview handlers to a single result card element
        function _attachResultHandlers(item, result, index) {
            const videoId = item.dataset.videoId;

            item.addEventListener('click', () => {
                if (!downloadingIds.has(videoId)) {
                    stopPreview();
                    downloadTrack(result, item);
                }
            });

            item.addEventListener('mouseenter', () => {
                if (!downloadingIds.has(videoId) && sourceHasPreview(result.source)) {
                    startHoverTimer(videoId, item, result);
                }
            });

            item.addEventListener('mouseleave', () => stopHoverPreview());

            const previewBtn = item.querySelector('.preview-btn');
            if (previewBtn) {
                previewBtn.addEventListener('click', (e) => {
                    e.stopPropagation();
                    if (downloadingIds.has(videoId)) return;
                    if (previewIsActiveOrLoading(videoId, result)) {
                        stopPreview();
                    } else {
                        startPreview(videoId, item, result);
                    }
                });
            }
        }

        function _renderMissingTrackCandidate(result, index) {
            const rescueNote = missingTrackVersionsState.mode === 'job' && result.attempted
                ? '<span class="missing-track-candidate-note">Tried automatically</span>'
                : '';
            const selectable = result.selectable !== false;
            return `
                <div class="missing-track-candidate">
                    ${_renderOneResult(result, index)}
                    <div class="missing-track-candidate-actions">
                        ${rescueNote}
                        <button type="button"
                            class="missing-track-candidate-btn"
                            data-action="queue-missing-track-candidate"
                            data-index="${index}" ${selectable ? '' : 'disabled'}>${selectable ? 'Use This' : 'Search again'}</button>
                    </div>
                </div>
            `;
        }

        function _attachMissingTrackCandidateHandlers(wrapper, result, index) {
            const item = wrapper.querySelector('.result-item');
            if (item) {
                const videoId = item.dataset.videoId;
                item.addEventListener('mouseenter', () => {
                    if (sourceHasPreview(result.source)) {
                        startHoverTimer(videoId, item, result);
                    }
                });
                item.addEventListener('mouseleave', () => stopHoverPreview());

                const previewBtn = item.querySelector('.preview-btn');
                if (previewBtn) {
                    previewBtn.addEventListener('click', (e) => {
                        e.stopPropagation();
                        if (previewIsActiveOrLoading(videoId, result)) {
                            stopPreview();
                        } else {
                            startPreview(videoId, item, result);
                        }
                    });
                }
            }

            const btn = wrapper.querySelector('[data-action="queue-missing-track-candidate"]');
            if (btn) {
                btn.addEventListener('click', (e) => {
                    e.preventDefault();
                    e.stopPropagation();
                    queueMissingTrackCandidate(index, btn);
                });
            }
        }

        function getSourceBadge(source) {
            const badges = { youtube: 'YT', mp3phoenix: 'PX', soundcloud: 'SC', zvu4no: 'ZV', freemp3cloud: 'FMC', soulseek: 'SLK', monochrome: 'MONO' };
            return badges[source] || source.toUpperCase().slice(0, 3);
        }

        function getSourceLabel(source) {
            const labels = { youtube: 'YouTube', mp3phoenix: 'MP3Phoenix', soundcloud: 'SoundCloud', zvu4no: 'zvu4no', freemp3cloud: 'FreeMp3Cloud', soulseek: 'Soulseek', monochrome: 'Monochrome' };
            return labels[source] || source;
        }

        function getQualityBadgeClass(quality) {
            if (!quality) return '';
            const q = quality.toUpperCase();
            if (q === 'HI_RES_LOSSLESS') return 'hires';
            if (q === 'LOSSLESS' || q.includes('FLAC')) return 'flac';
            if (q === 'HIGH' || q.includes('320') || q.includes('256')) return 'mp3-high';
            return '';
        }

        function formatQualityLabel(quality) {
            if (!quality) return '';
            const labels = {
                'HI_RES_LOSSLESS': 'Hi-Res',
                'LOSSLESS': 'Lossless',
                'HIGH': 'HQ',
            };
            return labels[quality] || quality;
        }

        function formatResultMeta(result) {
            const parts = [];
            const artist = (result.artist || '').trim();
            const uploader = (result.channel || '').trim();

            // For Soulseek the "channel" is whichever stranger happens to be sharing
            // the file, not the performer, so it goes down with the badges instead of
            // sitting up here impersonating an artist.
            if (result.source === 'soulseek') {
                if (artist) parts.push(escapeHtml(artist));
            } else if (artist) {
                parts.push(uploader && uploader !== artist
                    ? `${escapeHtml(artist)} • ${escapeHtml(uploader)}`
                    : escapeHtml(artist));
            } else if (uploader) {
                parts.push(escapeHtml(uploader));
            }

            // Show album if known - clickable to open in the Artists tab's album browser
            if (result.album) {
                const albumArtist = escapeAttr(artist || uploader);
                const albumTitle = escapeAttr(result.album);
                const year = result.year ? ` <span class="result-year">(${escapeHtml(String(result.year))})</span>` : '';
                parts.push(`<span class="album-link" data-artist="${albumArtist}" data-album="${albumTitle}" title="View album in Artists tab">${escapeHtml(result.album)}</span>${year}`);
            }
            return parts.filter(Boolean).join(' • ');
        }

        function formatFileSize(bytes) {
            if (!bytes || bytes <= 0) return '';
            const mb = bytes / (1024 * 1024);
            return mb >= 1024 ? `${(mb / 1024).toFixed(1)} GB` : `${mb.toFixed(1)} MB`;
        }

        // Soulseek results carry a real 0-1 confidence from the path scorer, so the
        // number means something. Colour it, because "62%" and "97%" deserve rather
        // different levels of enthusiasm.
        function matchStrengthClass(confidence) {
            if (confidence >= 0.9) return 'match-strong';
            if (confidence >= 0.75) return 'match-fair';
            return 'match-weak';
        }

        function renderMatchBadge(result) {
            if (typeof result.match_confidence !== 'number') return '';
            const percent = Math.round(result.match_confidence * 100);
            return `<div class="result-match ${matchStrengthClass(result.match_confidence)}" title="How closely this file's path matches what you searched for">${percent}%<span class="result-match-caption">match</span></div>`;
        }

        function renderResults(results) {
            // Store results for later access (needed for slskd fields on download)
            lastResults = results;

            if (!results.length) {
                resultsTab.innerHTML = `
                    <div class="empty-state">
                        <div class="empty-state-icon"><i class="fa-solid fa-magnifying-glass"></i></div>
                        <p>No results found</p>
                    </div>
                `;
                return;
            }

            // Show the destination picker row now that there are results to act on
            const _destRow2 = document.getElementById('destinationPickerRow');
            if (_destRow2) _destRow2.style.display = '';

            resultsTab.innerHTML = results.map((r, index) => {
                const safeSource = escapeHtml(r.source || '');
                const safeVideoId = escapeHtml(r.video_id || '');
                const previewable = sourceHasPreview(r.source);
                return `
                <div class="result-item ${downloadingIds.has(r.video_id) ? 'downloading' : ''} ${r.source === 'soulseek' ? 'soulseek' : ''}"
                     data-video-id="${safeVideoId}"
                     data-quality-tier="${Number(r.quality_tier) || 0}"
                     data-index="${index}"
                     data-title="${escapeHtml(r.title)}"
                     data-tooltip="${resultTooltip(previewable)}">
                    ${previewable ? '<div class="preview-indicator">▶</div>' : ''}
                    ${r.thumbnail ? `<img class="result-thumb" src="${escapeHtml(r.thumbnail)}" alt="" loading="lazy">` : '<div class="result-thumb"></div>'}
                    <div class="result-info">
                        <div class="result-title">${escapeHtml(r.title)}</div>
                        <div class="result-meta">${formatResultMeta(r)}</div>
                        <div class="result-badges">
                            <span class="source-badge ${safeSource}">${getSourceBadge(r.source)}</span>
                            ${r.quality ? `<span class="quality-badge ${getQualityBadgeClass(r.quality)}">${formatQualityLabel(r.quality)}</span>` : ''}
                            ${r.duration ? `<span class="result-duration">${r.duration}</span>` : ''}
                            ${r.bitrate ? `<span class="result-detail">${Number(r.bitrate)} kbps</span>` : ''}
                            ${r.size_bytes ? `<span class="result-detail">${formatFileSize(r.size_bytes)}</span>` : ''}
                            ${r.source === 'soulseek' && Number.isInteger(r.queue_length) ? `<span class="result-detail" title="Number of uploads already queued by this Soulseek peer">Peer queue: ${r.queue_length}</span>` : ''}
                            ${r.source === 'soulseek' && r.channel ? `<span class="result-uploader" title="The Soulseek user sharing this file">via ${escapeHtml(r.channel)}</span>` : ''}
                        </div>
                    </div>
                    ${renderMatchBadge(r)}
                    <div class="mobile-actions">
                        ${previewable ? `<button class="preview-btn" data-video-id="${safeVideoId}" data-index="${index}" title="Preview">Preview &#9654;</button>` : ''}
                        <button class="explore-btn" data-artist="${escapeAttr(r.artist || r.channel)}" title="Find similar artists via ListenBrainz">~ Similar</button>
                    </div>
                </div>
                `;
            }).join('');

            applyQualityFilter();
            applyCachedAlbumArtwork();

            // Add click and hover handlers
            resultsTab.querySelectorAll('.result-item').forEach(item => {
                const videoId = item.dataset.videoId;
                const index = parseInt(item.dataset.index);
                const result = lastResults[index];

                // Click to download
                item.addEventListener('click', () => {
                    if (!downloadingIds.has(videoId)) {
                        stopPreview();
                        downloadTrack(result, item);
                    }
                });

                // Hover to preview (desktop only, not for sources with no preview endpoint)
                item.addEventListener('mouseenter', () => {
                    if (!downloadingIds.has(videoId) && sourceHasPreview(result.source)) {
                        startHoverTimer(videoId, item, result);
                    }
                });

                item.addEventListener('mouseleave', () => {
                    stopHoverPreview();
                });

                // Mobile preview button - tap to preview without triggering download
                const previewBtn = item.querySelector('.preview-btn');
                if (previewBtn) {
                    previewBtn.addEventListener('click', (e) => {
                        e.stopPropagation();
                        if (downloadingIds.has(videoId)) return;
                        if (previewIsActiveOrLoading(videoId, result)) {
                            stopPreview();
                        } else {
                            startPreview(videoId, item, result);
                        }
                    });
                }

                // Similar artists button - explore via ListenBrainz
                const exploreBtn = item.querySelector('.explore-btn');
                if (exploreBtn) {
                    exploreBtn.addEventListener('click', (e) => {
                        e.stopPropagation();
                        exploreSimilar(exploreBtn.dataset.artist);
                    });
                }

                // Album link - click to open in the Artists tab's album browser
                const albumLink = item.querySelector('.album-link');
                if (albumLink) {
                    albumLink.addEventListener('click', (e) => {
                        e.stopPropagation();
                        e.preventDefault();
                        openAlbumFromSearch({
                            artistName: albumLink.dataset.artist,
                            albumTitle: albumLink.dataset.album
                        });
                    });
                }
            });
        }

        async function loadMissingTrackVersions() {
            const body = document.getElementById('missingTrackVersionsBody');
            const meta = document.getElementById('missingTrackVersionsMeta');
            if (!body || !meta) return;

            const isJobRescue = missingTrackVersionsState.mode === 'job';
            meta.textContent = isJobRescue
                ? `Failed download • ${missingTrackVersionsState.artist} - ${missingTrackVersionsState.title}`
                : `${missingTrackVersionsState.playlistName || 'Watched playlist'} • ${missingTrackVersionsState.artist} - ${missingTrackVersionsState.title}`;
            body.innerHTML = `<div class="missing-track-modal-empty">${isJobRescue ? 'Loading saved alternatives...' : 'Searching for alternatives...'}</div>`;

            try {
                let resp;
                if (isJobRescue) {
                    resp = await apiFetch(`/api/jobs/${encodeURIComponent(missingTrackVersionsState.jobId)}/rescue-candidates`);
                } else {
                    const params = new URLSearchParams({
                        artist: missingTrackVersionsState.artist,
                        title: missingTrackVersionsState.title,
                        limit: '4',
                    });
                    resp = await apiFetch(`/api/watched-playlists/${encodeURIComponent(missingTrackVersionsState.playlistId)}/track-candidates?${params.toString()}`);
                }
                if (!resp.ok) throw new Error('Search failed');
                const data = await resp.json();
                if (isJobRescue) {
                    missingTrackVersionsState.artist = data.artist || '';
                    missingTrackVersionsState.title = data.title || '';
                    meta.textContent = `Failed download • ${missingTrackVersionsState.artist} - ${missingTrackVersionsState.title}`;
                }
                const results = Array.isArray(isJobRescue ? data.candidates : data.results)
                    ? (isJobRescue ? data.candidates : data.results)
                    : [];
                missingTrackVersionsState.results = results;

                if (!results.length) {
                    body.innerHTML = `<div class="missing-track-modal-empty">${isJobRescue ? 'No saved alternatives remain. Use Search Again to ask the allowed sources for fresh options.' : 'No alternatives found. Retry will still run the automatic watched-playlist search, or Search will open the full Tracks tab.'}</div>`;
                    return;
                }

                body.innerHTML = results.map((result, index) => _renderMissingTrackCandidate(result, index)).join('');
                body.querySelectorAll('.missing-track-candidate').forEach((wrapper, index) => {
                    _attachMissingTrackCandidateHandlers(wrapper, results[index], index);
                });
            } catch (error) {
                body.innerHTML = '<div class="missing-track-modal-empty">Could not load alternatives right now.</div>';
            }
        }

        async function openMissingTrackVersionsModal(playlistId, playlistName, artist, title, rowId) {
            missingTrackVersionsState.mode = 'watched';
            missingTrackVersionsState.jobId = '';
            missingTrackVersionsState.playlistId = playlistId;
            missingTrackVersionsState.playlistName = playlistName;
            missingTrackVersionsState.artist = artist;
            missingTrackVersionsState.title = title;
            missingTrackVersionsState.rowId = rowId || '';
            missingTrackVersionsState.results = [];
            document.getElementById('missingTrackVersionsOverlay').style.display = 'flex';
            await loadMissingTrackVersions();
        }

        async function openJobRescueVersions(jobId) {
            missingTrackVersionsState.mode = 'job';
            missingTrackVersionsState.jobId = jobId;
            missingTrackVersionsState.playlistId = '';
            missingTrackVersionsState.playlistName = '';
            missingTrackVersionsState.artist = '';
            missingTrackVersionsState.title = '';
            missingTrackVersionsState.rowId = '';
            missingTrackVersionsState.results = [];
            document.querySelector('#missingTrackVersionsOverlay .modal-title').textContent = 'Choose Another Version';
            document.getElementById('missingTrackVersionsRefreshBtn').textContent = 'Search Again';
            document.getElementById('missingTrackVersionsOverlay').style.display = 'flex';
            await loadMissingTrackVersions();
        }

        function closeMissingTrackVersionsModal() {
            stopPreview();
            missingTrackVersionsState.mode = 'watched';
            missingTrackVersionsState.jobId = '';
            missingTrackVersionsState.playlistId = '';
            missingTrackVersionsState.playlistName = '';
            missingTrackVersionsState.artist = '';
            missingTrackVersionsState.title = '';
            missingTrackVersionsState.rowId = '';
            missingTrackVersionsState.results = [];
            document.querySelector('#missingTrackVersionsOverlay .modal-title').textContent = 'Find Versions';
            document.getElementById('missingTrackVersionsRefreshBtn').textContent = 'Refresh Results';
            document.getElementById('missingTrackVersionsOverlay').style.display = 'none';
        }

        async function queueMissingTrackCandidate(index, btn) {
            const result = missingTrackVersionsState.results[index];
            if (!result) return;

            const originalText = btn ? btn.textContent : '';
            if (btn) {
                btn.disabled = true;
                btn.textContent = 'Queueing...';
            }

            try {
                if (missingTrackVersionsState.mode === 'job') {
                    const resp = await apiFetch(`/api/jobs/${encodeURIComponent(missingTrackVersionsState.jobId)}/rescue-candidate`, {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ candidate_key: result.candidate_key })
                    });
                    if (!resp.ok) {
                        const err = await resp.json().catch(() => ({}));
                        throw new Error(err.detail || 'Failed to queue candidate');
                    }
                    showToast(`Queued ${missingTrackVersionsState.artist} - ${missingTrackVersionsState.title}`);
                    closeMissingTrackVersionsModal();
                    loadJobs(false);
                    return;
                }
                const payload = {
                    artist: missingTrackVersionsState.artist,
                    title: missingTrackVersionsState.title,
                    video_id: result.video_id || '',
                    source: result.source || 'youtube',
                    source_url: result.source_url || null,
                    slskd_username: result.slskd_username || null,
                    slskd_filename: result.slskd_filename || null,
                    slskd_size: result.slskd_size || result.size || null,
                };
                const resp = await apiFetch(`/api/watched-playlists/${encodeURIComponent(missingTrackVersionsState.playlistId)}/queue-track-candidate`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload)
                });
                if (!resp.ok) {
                    const err = await resp.json().catch(() => ({}));
                    throw new Error(err.detail || 'Failed to queue candidate');
                }

                const row = missingTrackVersionsState.rowId ? document.getElementById(missingTrackVersionsState.rowId) : null;
                if (row) {
                    const retryBtn = row.querySelector('[data-action="retry-missing-track"]');
                    if (retryBtn) {
                        retryBtn.disabled = true;
                        retryBtn.textContent = 'Queued';
                    }
                }

                showToast(`Queued ${missingTrackVersionsState.artist} - ${missingTrackVersionsState.title}`);
                closeMissingTrackVersionsModal();
                if (document.querySelector('.tab.active[data-tab="queue"]')) {
                    loadJobs(false);
                }
            } catch (error) {
                showToast(error?.message || 'Failed to queue candidate', true);
                if (btn) {
                    btn.disabled = false;
                    btn.textContent = originalText || 'Use This';
                }
            }
        }

        function resultDurationSeconds(value) {
            if (typeof value === 'number') {
                return Number.isFinite(value) && value > 0 ? value : null;
            }
            const text = String(value || '').trim();
            if (!text) return null;
            if (/^\d+$/.test(text)) return Number(text) || null;
            const parts = text.split(':');
            if (parts.length < 2 || parts.length > 3 || parts.some(part => !/^\d+$/.test(part))) {
                return null;
            }
            const seconds = parts.reduce((total, part) => total * 60 + Number(part), 0);
            return seconds > 0 ? seconds : null;
        }

        async function downloadTrack(result, element) {
            // Check queue size limit
            const queueSize = await getQueueSize();
            if (queueSize >= MAX_QUEUE_SIZE) {
                showToast(`Queue limit reached (max ${MAX_QUEUE_SIZE})`, true);
                return;
            }

            downloadingIds.add(result.video_id);
            element.classList.add('downloading');

            showToast(`Processing (${getSourceLabel(result.source)})...`, false);

            try {
                let queuedTitle = result.title;
                const payload = {
                    video_id: result.video_id,
                    title: queuedTitle,
                    convert_audio: convertAudioCheckbox.checked,
                    source: result.source || 'youtube',
                    search_token: currentSearchLogToken
                };
                const selectedDurationSecs = resultDurationSeconds(result.duration);
                if (selectedDurationSecs) {
                    payload.selected_duration_secs = selectedDurationSecs;
                }
                if (result.artist || result.channel) {
                    payload.artist = result.artist || result.channel;
                }

                // URL-based sources need the full URL for downloading
                if ((result.source === 'soundcloud' || result.source === 'zvu4no' || result.source === 'freemp3cloud' || result.source === 'monochrome') && result.source_url) {
                    payload.source_url = result.source_url;
                }

                // Add slskd-specific fields if this is a Soulseek result
                if (result.source === 'soulseek') {
                    payload.slskd_username = result.slskd_username;
                    payload.slskd_filename = result.slskd_filename;
                    payload.slskd_size = result.slskd_size || result.size || null;
                    if (result.artist) {
                        payload.artist = result.artist;
                    }
                }

                const albumRoute = getSelectedAlbumRoute();
                if (_destinationMode === 'album' && !albumRoute) {
                    throw new Error('Select an artist and album folder before downloading.');
                }
                if (albumRoute) {
                    if (albumRoute.mode === 'no_mbid') {
                        // Folder routing only, no .albuminfo found so no track metadata enrichment
                        payload.album_artist = albumRoute.album_artist;
                        payload.album_name = albumRoute.album_name;
                    } else {
                        let matchedTrack = null;
                        if (albumRoute.mode === 'manual') {
                            matchedTrack = {
                                track_title: albumRoute.track_title,
                                track_number: albumRoute.track_number,
                                track_total: albumRoute.track_total,
                            };
                        } else {
                            const matchResp = await apiFetch(`/api/albums/release/${encodeURIComponent(albumRoute.release_mbid)}/match-track`, {
                                method: 'POST',
                                headers: { 'Content-Type': 'application/json' },
                                body: JSON.stringify({
                                    artist: result.artist || result.channel || '',
                                    title: result.title || '',
                                    album_artist: albumRoute.album_artist || '',
                                })
                            });
                            if (!matchResp.ok) {
                                const err = await matchResp.json().catch(() => ({}));
                                throw new Error(err.detail || 'No album-track match found');
                            }
                            matchedTrack = await matchResp.json();
                        }

                        queuedTitle = matchedTrack.track_title || queuedTitle;
                        payload.title = queuedTitle;
                        payload.album_release_mbid = albumRoute.release_mbid;
                        payload.album_artist = albumRoute.album_artist;
                        payload.album_name = albumRoute.album_name;
                        payload.album_track_title = matchedTrack.track_title || queuedTitle;
                        payload.album_track_number = matchedTrack.track_number || null;
                        payload.album_track_total = matchedTrack.track_total || albumRoute.track_total || null;
                    }
                }

                // Playlist routing - attach target playlist if one is selected
                const selectedPlaylist = getSelectedPlaylist();
                if (selectedPlaylist && albumRoute) {
                    throw new Error('Choose either Add to playlist or Add to album, not both');
                }
                if (selectedPlaylist) {
                    payload.playlist_name = selectedPlaylist.name;
                    payload.use_playlists_dir = true;
                }

                const response = await apiFetch('/api/download', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload)
                });

                if (!response.ok) throw new Error('Download failed');

                const data = await response.json();

                // Update toast with success message
                const formatMsg = convertAudioCheckbox.checked ? ` (${conversionChoiceLabel(true)})` : ' (original format)';
                const qualityMsg = result.quality ? ` [${formatQualityLabel(result.quality)}]` : '';
                const pl = getSelectedPlaylist();
                const playlistMsg = pl ? ` → ${pl.name}` : '';
                const al = getSelectedAlbumRoute();
                const albumMsg = al ? ` → ${al.album_name}` : '';
                showToast(`Added to queue${qualityMsg}${formatMsg}${playlistMsg}${albumMsg}`);
            } catch (error) {
                downloadingIds.delete(result.video_id);
                element.classList.remove('downloading');
                showToast(error?.message ? `Failed to queue: ${error.message}` : 'Failed to queue', true);
            }
        }

        function activeCountFromTotals() {
            if (!queueTotals) return null;
            return (queueTotals.queued || 0) + (queueTotals.downloading || 0);
        }

        async function getQueueSize() {
            // Server totals when we have them: counting the cached page of jobs
            // undercounts the moment a search is on, or the queue outgrows the window.
            const fromTotals = activeCountFromTotals();
            if (fromTotals !== null) return fromTotals;
            const jobs = allQueueJobs.length > 0 && !queueSearchTerm ? allQueueJobs : await (async () => {
                try {
                    const response = await apiFetch('/api/jobs?limit=250');
                    if (!response.ok) return [];
                    const data = await response.json();
                    if (data.queue_totals) queueTotals = data.queue_totals;
                    return data.jobs;
                } catch { return []; }
            })();
            const retry = activeCountFromTotals();
            if (retry !== null) return retry;
            return jobs.filter(j => j.status === 'queued' || j.status === 'downloading').length;
        }

        function stopQueuePolling() {
            if (queuePollInterval) {
                clearTimeout(queuePollInterval);
                queuePollInterval = null;
            }
            if (queueElapsedInterval) {
                clearInterval(queueElapsedInterval);
                queueElapsedInterval = null;
            }
        }

        function scheduleQueuePoll() {
            if (currentTab !== 'queue') return;
            if (queuePollInterval) clearTimeout(queuePollInterval);
            const activeTotal = activeCountFromTotals();
            const hasActiveJobs = activeTotal !== null
                ? activeTotal > 0
                : allQueueJobs.some(job => ['queued', 'downloading'].includes(job.status));
            // Stay snappy while work is moving, then back off when the queue is idle.
            const delay = hasActiveJobs ? 1500 : 5000;
            queuePollInterval = setTimeout(async () => {
                queuePollInterval = null;
                await loadJobs(false);
                scheduleQueuePoll();
            }, delay);
        }

        async function loadJobs(showLoading = true) {
            if (queueLoadInFlight) return;
            queueLoadInFlight = true;
            const requestedTerm = queueSearchTerm;
            if (showLoading) {
                queueTab.innerHTML = `
                    <div class="loading loading-with-copy" role="status">
                        <div class="spinner"></div>
                        <span>Taking attendance…</span>
                    </div>
                `;
            }

            try {
                const url = requestedTerm
                    ? `/api/jobs?limit=${QUEUE_SEARCH_LIMIT}&q=${encodeURIComponent(requestedTerm)}`
                    : `/api/jobs?limit=${QUEUE_SEARCH_LIMIT}`;
                const response = await apiFetch(url);
                if (!response.ok) throw new Error('Failed to load jobs');

                const data = await response.json();
                if (requestedTerm !== queueSearchTerm) return;
                allQueueJobs = data.jobs;
                queueTotals = data.queue_totals || null;
                queueLastUpdatedAt = Date.now();
                // Clamp page in case jobs were cleared
                const totalPages = Math.max(1, Math.ceil(allQueueJobs.length / QUEUE_PAGE_SIZE));
                if (queuePage > totalPages) queuePage = totalPages;
                renderQueueSummary();
                renderQueuePage();
            } catch (error) {
                if (showLoading && requestedTerm === queueSearchTerm) {
                    queueTab.innerHTML = `
                        <div class="empty-state">
                            <div class="empty-state-icon"><i class="fa-solid fa-circle-exclamation"></i></div>
                            <p>Failed to load queue</p>
                        </div>
                    `;
                }
            } finally {
                queueLoadInFlight = false;
                // A search changed while we waited; fetch it now, however long
                // the previous request took to make up its mind.
                if (requestedTerm !== queueSearchTerm) await loadJobs(false);
            }
        }

        function secondsSince(isoString) {
            if (!isoString) return 0;
            const time = new Date(isoString).getTime();
            return Number.isFinite(time) ? Math.max(0, Math.floor((Date.now() - time) / 1000)) : 0;
        }

        function formatElapsedSeconds(seconds) {
            const secs = Math.max(0, Math.floor(seconds || 0));
            if (secs < 60) return `${secs}s`;
            const mins = Math.floor(secs / 60);
            if (mins < 60) return `${mins}m ${String(secs % 60).padStart(2, '0')}s`;
            const hours = Math.floor(mins / 60);
            return `${hours}h ${String(mins % 60).padStart(2, '0')}m`;
        }

        function _queueQuip(section, fallback) {
            const pool = (typeof QUEUE_QUIPS !== 'undefined' && QUEUE_QUIPS[section]) || null;
            return _randomQuip(pool, fallback);
        }

        function renderQueueSummary() {
            if (!queueLiveSummary) return;
            const totalJobs = queueTotals
                ? Object.values(queueTotals).reduce((sum, n) => sum + n, 0)
                : allQueueJobs.length;
            if (!totalJobs) {
                queueLiveSummary.style.display = 'none';
                queueSummarySignature = '';
                return;
            }
            const downloading = queueTotals
                ? (queueTotals.downloading || 0)
                : allQueueJobs.filter(job => job.status === 'downloading').length;
            const queued = queueTotals
                ? (queueTotals.queued || 0)
                : allQueueJobs.filter(job => job.status === 'queued').length;
            const active = downloading + queued;
            const signature = `${downloading}:${queued}`;
            if (signature !== queueSummarySignature) {
                let headline;
                let aside;
                if (downloading && queued) {
                    headline = `${downloading} on the workbench · ${queued} waiting`;
                    aside = _queueQuip('both', 'The conveyor belt is earning its keep.');
                } else if (downloading) {
                    headline = `${downloading} track${downloading === 1 ? '' : 's'} on the workbench`;
                    aside = _queueQuip('downloading', 'Tiny hammers, serious business.');
                } else if (queued) {
                    headline = `${queued} track${queued === 1 ? '' : 's'} waiting its turn`;
                    aside = _queueQuip('queued', 'Forming an orderly queue, naturally.');
                } else {
                    headline = 'All caught up';
                    aside = _queueQuip('idle', 'The queue has put its feet up.');
                }
                queueLiveSummary.innerHTML = `
                    <span class="queue-live-dot ${active ? 'active' : ''}" aria-hidden="true"></span>
                    <span class="queue-live-copy">
                        <strong>${headline}</strong>
                        <span>${aside}</span>
                    </span>
                    <span class="queue-live-updated" aria-hidden="true">Live · checked just now</span>
                `;
                queueSummarySignature = signature;
            }
            queueLiveSummary.style.display = 'flex';
            updateLiveQueueTimes();
        }

        function updateLiveQueueTimes() {
            document.querySelectorAll('.job-elapsed[data-start]').forEach(element => {
                element.textContent = formatElapsedSeconds(secondsSince(element.dataset.start));
            });
            const updated = queueLiveSummary?.querySelector('.queue-live-updated');
            if (updated && queueLastUpdatedAt) {
                const age = Math.floor((Date.now() - queueLastUpdatedAt) / 1000);
                const nextText = age < 2 ? 'Live · checked just now' : `Live · checked ${age}s ago`;
                if (updated.textContent !== nextText) {
                    updated.classList.add('is-changing');
                    setTimeout(() => {
                        if (!updated.isConnected) return;
                        updated.textContent = nextText;
                        updated.classList.remove('is-changing');
                    }, 140);
                }
            }
        }

        function renderQueuePage() {
            const displayJobs = getSortedQueueJobs();
            const totalPages = Math.max(1, Math.ceil(displayJobs.length / QUEUE_PAGE_SIZE));
            const start = (queuePage - 1) * QUEUE_PAGE_SIZE;
            const pageJobs = displayJobs.slice(start, start + QUEUE_PAGE_SIZE);
            renderJobs(pageJobs);

            const pagerEl = document.getElementById('queuePager');
            const infoEl = document.getElementById('queuePagerInfo');
            const prevBtn = document.getElementById('queuePrevBtn');
            const nextBtn = document.getElementById('queueNextBtn');
            if (allQueueJobs.length > 1) {
                pagerEl.style.display = 'flex';
                const noun = queueSearchTerm ? 'matches' : 'jobs';
                infoEl.textContent = `Page ${queuePage} of ${totalPages} (${allQueueJobs.length} ${noun})`;
                prevBtn.disabled = queuePage <= 1;
                nextBtn.disabled = queuePage >= totalPages;
            } else {
                pagerEl.style.display = 'none';
            }

            renderQueueSearchStatus();
        }

        function renderQueueSearchStatus() {
            const statusEl = document.getElementById('queueSearchStatus');
            if (!statusEl) return;
            if (!queueSearchTerm) {
                statusEl.style.display = 'none';
                statusEl.innerHTML = '';
                return;
            }
            const count = allQueueJobs.length;
            const term = escapeHtml(queueSearchTerm);
            if (!count) {
                statusEl.innerHTML = `Nothing in the queue matches &ldquo;${term}&rdquo;.`;
            } else if (count >= QUEUE_SEARCH_LIMIT) {
                // Claiming "250 matches" when we stopped counting at 250 is the sort
                // of small fib that gets found out.
                statusEl.innerHTML = `First <strong>${QUEUE_SEARCH_LIMIT}</strong> matches for &ldquo;${term}&rdquo;. Add a word or two to narrow it down.`;
            } else {
                statusEl.innerHTML = `<strong>${count}</strong> match${count === 1 ? '' : 'es'} for &ldquo;${term}&rdquo; across the whole queue.`;
            }
            statusEl.style.display = 'block';
        }

        function getSortedQueueJobs() {
            const isLive = job => ['queued', 'downloading'].includes(job.status);
            const newestFirst = (left, right) => String(right.created_at || '').localeCompare(String(left.created_at || ''));
            const liveJobs = allQueueJobs
                .filter(isLive)
                .sort((left, right) => {
                    const statusOrder = (left.status === 'downloading' ? 0 : 1) - (right.status === 'downloading' ? 0 : 1);
                    return statusOrder || newestFirst(left, right);
                });
            const historyJobs = allQueueJobs.filter(job => !isLive(job));

            if (queueHistorySortMode === 'recent') {
                return [...liveJobs, ...historyJobs];
            }

            return [
                ...liveJobs,
                ...historyJobs.filter(job => job.status === queueHistorySortMode),
                ...historyJobs.filter(job => job.status !== queueHistorySortMode),
            ];
        }

        function qualityDetailRows(quality) {
            if (!quality) return '';
            const match = String(quality).match(/^(.*?)\s+\(from\s+(.+)\)$/i);
            if (!match) {
                return `<div class="job-details-row"><span class="job-details-label">Stored:</span> ${escapeHtml(quality)}</div>`;
            }
            return `
                <div class="job-details-row"><span class="job-details-label">Stored:</span> ${escapeHtml(match[1])}</div>
                <div class="job-details-row"><span class="job-details-label">Original:</span> ${escapeHtml(match[2])}</div>
                <div class="job-details-row"><span class="job-details-label">Class:</span> Lossy source, transcoded</div>`;
        }

        function changeQueuePage(delta) {
            const totalPages = Math.max(1, Math.ceil(getSortedQueueJobs().length / QUEUE_PAGE_SIZE));
            queuePage = Math.max(1, Math.min(totalPages, queuePage + delta));
            renderQueuePage();
        }

        function renderJobs(jobs, container = queueTab) {
            if (!jobs.length) {
                if (container === queueTab) expandedJobIds.clear();
                // A search that found nothing is not an empty queue, and saying so
                // has sent more than one person hunting for a bug that isn't there.
                if (container === queueTab && queueSearchTerm) {
                    container.innerHTML = `
                        <div class="empty-state">
                            <div class="empty-state-icon"><i class="fa-solid fa-magnifying-glass"></i></div>
                            <p>No matches for &ldquo;${escapeHtml(queueSearchTerm)}&rdquo;</p>
                            <small>Try less of the title, or check the spelling. We all do it.</small>
                        </div>
                    `;
                    return;
                }
                container.innerHTML = `
                    <div class="empty-state">
                        <div class="empty-state-icon"><i class="fa-solid fa-inbox"></i></div>
                        <p>No downloads yet</p>
                        <small>Quiet in here. Suspiciously quiet.</small>
                    </div>
                `;
                return;
            }

            const visibleJobIds = new Set(jobs.map(j => j.id));
            if (container === queueTab) {
                // expandedJobIds tracks the Queue tab's own card state; a render
                // into a different container (e.g. Bulk Import) must not prune
                // ids just because they're not part of its own smaller job set.
                for (const id of Array.from(expandedJobIds)) {
                    if (!visibleJobIds.has(id)) expandedJobIds.delete(id);
                }
            }

            container.innerHTML = jobs.map(job => {
                const hasDetails = job.status === 'completed' || job.status === 'completed_with_errors' || job.status === 'failed';
                const isExpanded = hasDetails && expandedJobIds.has(job.id);
                const sourceLabel = getSourceLabel(job.source || 'youtube');
                const sourceUrl = job.source_url || '';
                const isClickableUrl = sourceUrl.startsWith('https://');
                const fileDeleted = Number(job.file_deleted || 0) === 1;
                const isActive = job.status === 'queued' || job.status === 'downloading';
                const activeStage = job.status === 'queued'
                    ? 'Waiting its turn'
                    : (job.progress_stage || 'Getting organised');
                const elapsedHtml = isActive
                    ? `<span class="job-elapsed-wrap">for <span class="job-elapsed" data-start="${escapeAttr(job.created_at || '')}">${formatElapsedSeconds(secondsSince(job.created_at))}</span></span>`
                    : '';
                const acquisitionAttempts = Array.isArray(job.acquisition_attempts)
                    ? job.acquisition_attempts.filter(a => a && a.source)
                    : [];
                const sourceJourney = acquisitionAttempts.length
                    ? acquisitionAttempts.map(a => a.source)
                    : (Array.isArray(job.source_history) ? job.source_history.filter(Boolean) : []);
                const journeyHtml = sourceJourney.length > 1
                    ? sourceJourney.map((source, index) =>
                        `${index ? '<span class="job-journey-arrow">→</span>' : ''}<span class="source-badge ${escapeHtml(source)}">${getSourceBadge(source)}</span>`
                    ).join('')
                    : '';
                const attemptHistoryHtml = acquisitionAttempts.length
                    ? acquisitionAttempts.slice(-8).map(attempt => `
                        <div class="job-details-row">
                            <span class="job-details-label">Cycle ${escapeHtml(String(attempt.cycle_number || '?'))}:</span>
                            <span class="source-badge ${escapeHtml(attempt.source || '')}">${getSourceBadge(attempt.source || '')}</span>
                            ${escapeHtml(attempt.status || '')}${attempt.error ? ` — ${escapeHtml(attempt.error)}` : ''}
                        </div>`).join('')
                    : '';
                return `
                <div class="job-item ${hasDetails ? 'has-details' : ''} ${isExpanded ? 'expanded' : ''}" data-job-id="${escapeHtml(job.id || '')}" ${hasDetails ? 'onclick="toggleJobDetails(this)"' : ''}>
                    <div class="job-status ${job.status}"></div>
                    <div class="job-info">
                        <div class="job-title">${escapeHtml(job.artist ? `${job.artist} - ${job.title}` : job.title)}${job.status === 'completed_with_errors' ? '<span class="job-warning-badge">ISSUES</span>' : ''}${job.download_type === 'upgrade' ? '<span class="job-upgrade-badge">UPGRADE</span>' : ''}</div>
                        <div class="job-meta">
                            <span class="job-status-label">${escapeHtml(formatJobStatus(job.status))}</span>
                            ${isActive ? `<span class="job-progress-stage">${escapeHtml(activeStage)}</span>${elapsedHtml}` : ''}
                            <span class="job-date">· ${formatTime(job.created_at)}</span>
                        </div>
                        ${job.error ? (job.error.startsWith('Already exists') ? '<div class="job-error job-note">Already in library</div>' : `<div class="job-error">${escapeHtml(job.error)}</div>`) : ''}
                        ${hasDetails ? `
                        <div class="job-details" style="display:${isExpanded ? 'block' : 'none'};">
                            ${qualityDetailRows(job.audio_quality)}
                            ${job.error && job.error.startsWith('Already exists') ? `<div class="job-details-row"><span class="job-details-label">Path:</span> <span class="job-details-url">${escapeHtml(job.error.replace(/^Already exists(?: in [^:]+)?:\s*/, '').replace(/ \(added to playlist\)$/, ''))}</span></div>` : ''}
                            <div class="job-details-row"><span class="job-details-label">Source:</span> ${escapeHtml(sourceLabel)}</div>
                            ${journeyHtml ? `<div class="job-details-row job-journey"><span class="job-details-label">Journey:</span> ${journeyHtml}</div>` : ''}
                            ${job.acquisition_attempt_count ? `<div class="job-details-row"><span class="job-details-label">Acquisition:</span> ${escapeHtml(String(job.acquisition_cycle_count || 0))} cycle${Number(job.acquisition_cycle_count || 0) === 1 ? '' : 's'} · ${escapeHtml(String(job.acquisition_attempt_count))} source attempt${Number(job.acquisition_attempt_count) === 1 ? '' : 's'}${job.acquisition_last_attempt_at ? ` · last ${formatTime(job.acquisition_last_attempt_at)}` : ''}</div>` : ''}
                            ${attemptHistoryHtml}
                            ${job.acquisition_superseded_by ? `<div class="job-details-row job-superseded-note"><i class="fa-solid fa-circle-check"></i> Later succeeded on retry — <a href="#" onclick="event.preventDefault(); event.stopPropagation(); jumpToJob('${escapeAttr(job.acquisition_superseded_by)}')">jump to completed entry</a></div>` : ''}
                            ${job.metadata_source ? `<div class="job-details-row"><span class="job-details-label">Metadata:</span> ${escapeHtml(formatMetadataSource(job.metadata_source))}</div>` : ''}
                            ${job.requested_video_fallback ? '<div class="job-details-row"><span class="job-details-label">Fallback:</span> Used the video from the original YouTube playlist after no confident alternative succeeded.</div>' : ''}
                            ${sourceUrl ? `<div class="job-details-row"><span class="job-details-label">URL:</span> ${isClickableUrl ? `<a href="${escapeHtml(sourceUrl)}" target="_blank" rel="noopener" onclick="event.stopPropagation()">${escapeHtml(sourceUrl)}</a>` : `<span class="job-details-url">${escapeHtml(sourceUrl)}</span>`}</div>` : ''}
                            <div class="job-details-row"><span class="job-details-label">Queued:</span> ${formatTimeFull(job.created_at)}</div>
                            ${job.completed_at ? `<div class="job-details-row"><span class="job-details-label">Completed:</span> ${formatTimeFull(job.completed_at)}</div>` : ''}
                            ${job.completed_at && job.created_at ? `<div class="job-details-row"><span class="job-details-label">Took:</span> ${formatDuration(job.created_at, job.completed_at)}</div>` : ''}
                            ${['failed', 'completed_with_errors'].includes(job.status) || (job.error || '').includes('mismatch') ? `
                            <div id="score-rationale-link-${escapeHtml(job.id || '')}" class="job-details-row" style="margin-top:4px;">
                                <a href="#" onclick="event.preventDefault(); event.stopPropagation(); loadScoreRationale('${escapeAttr(job.id || '')}')" style="font-size:12px; color:var(--accent); text-decoration:none;">Why this result?</a>
                                <button class="score-rationale-copy" onclick="event.stopPropagation(); copyScoreRationale('${escapeAttr(job.id || '')}')" style="margin-left:8px;" title="Copy scoring rationale to clipboard"><i class="fa-solid fa-clipboard"></i> Copy</button>
                            </div>
                            <div id="score-rationale-${escapeHtml(job.id || '')}" style="display:none;"></div>
                            ` : ''}
                            <div style="display: flex; gap: 8px; margin-top: 8px; flex-wrap: wrap;">
                                ${job.status !== 'failed' && !fileDeleted ? `<button class="library-play-btn" aria-label="Play or pause" title="Play or pause" onclick="event.stopPropagation(); playJobAudio('${escapeAttr(job.id || '')}', this)" style="padding: 6px 12px; font-size: 11px; font-family: inherit; font-weight: 600; background: var(--bg-tertiary); color: var(--accent); border: 1px solid var(--border); border-radius: 6px; cursor: pointer;"><i class="fa-solid fa-play"></i></button>` : ''}
                                ${job.status === 'failed' && job.acquisition_mode === 'automatic'
                                    ? `<button onclick="event.stopPropagation(); retryNextJob('${escapeAttr(job.id || '')}')" style="padding: 6px 12px; font-size: 11px; font-family: inherit; font-weight: 600; background: var(--bg-tertiary); color: var(--accent); border: 1px solid var(--border); border-radius: 6px; cursor: pointer;">Retry next option</button>
                                       <button onclick="event.stopPropagation(); openJobRescueVersions('${escapeAttr(job.id || '')}')" style="padding: 6px 12px; font-size: 11px; font-family: inherit; font-weight: 600; background: var(--bg-tertiary); color: var(--text-secondary); border: 1px solid var(--border); border-radius: 6px; cursor: pointer;">Choose version</button>`
                                    : (job.download_type === 'upgrade' ? '' : `<button onclick="event.stopPropagation(); redownloadJob('${escapeAttr(job.id || '')}')" style="padding: 6px 12px; font-size: 11px; font-family: inherit; font-weight: 600; background: var(--bg-tertiary); color: var(--accent); border: 1px solid var(--border); border-radius: 6px; cursor: pointer;">Re-download</button>`)}
                                <button class="report-btn" data-job-id="${escapeHtml(job.id || '')}" data-video-id="${escapeHtml(job.video_id || '')}" data-uploader="${escapeHtml(job.uploader || '')}" data-source="${escapeHtml(job.source || 'youtube')}" onclick="event.stopPropagation()" style="padding: 6px 12px; font-size: 11px; font-family: inherit; font-weight: 600; background: var(--bg-tertiary); color: var(--warning); border: 1px solid var(--border); border-radius: 6px; cursor: pointer;">Report</button>
                                ${(job.error || '').includes('mismatch') ? `<button class="force-accept-btn" data-job-id="${escapeHtml(job.id || '')}" onclick="event.stopPropagation()" style="padding: 6px 12px; font-size: 11px; font-family: inherit; font-weight: 600; background: var(--bg-tertiary); color: var(--accent); border: 1px solid var(--border); border-radius: 6px; cursor: pointer;">Force Download</button>` : ''}
                                ${(job.status === 'completed' || job.status === 'completed_with_errors') && !fileDeleted ? `<button class="edit-tags-btn" data-job-id="${escapeHtml(job.id || '')}" data-artist="${escapeAttr(job.artist || '')}" data-title="${escapeAttr(job.title || '')}" data-album="${escapeAttr(job.album_name || '')}" onclick="event.stopPropagation()" style="padding: 6px 12px; font-size: 11px; font-family: inherit; font-weight: 600; background: var(--bg-tertiary); color: var(--accent); border: 1px solid var(--border); border-radius: 6px; cursor: pointer;">Edit Tags</button>` : ''}
                                ${job.status !== 'failed' ? (fileDeleted
                                    ? `<button disabled style="padding: 6px 12px; font-size: 11px; font-family: inherit; font-weight: 600; background: var(--bg-secondary); color: var(--text-muted); border: 1px solid var(--border); border-radius: 6px; cursor: not-allowed; opacity: 0.8;">Trashed</button>`
                                    : `<button class="delete-file-btn" data-job-id="${escapeHtml(job.id || '')}" data-track-name="${escapeHtml(job.artist ? job.artist + ' - ' + job.title : job.title)}" onclick="event.stopPropagation()" style="padding: 6px 12px; font-size: 11px; font-family: inherit; font-weight: 600; background: var(--bg-tertiary); color: var(--error); border: 1px solid var(--border); border-radius: 6px; cursor: pointer;"><i class="fa-solid fa-trash-can"></i> Trash</button>
                                      <button onclick="event.stopPropagation(); saveJobToDevice('${escapeAttr(job.id || '')}')" style="padding: 6px 12px; font-size: 11px; font-family: inherit; font-weight: 600; background: var(--bg-tertiary); color: var(--text-secondary); border: 1px solid var(--border); border-radius: 6px; cursor: pointer;"><i class="fa-solid fa-download"></i> Save to device</button>`) : ''}
                            </div>
                        </div>` : ''}
                    </div>
                    <span class="source-badge job-source-badge ${job.source || 'youtube'}">${getSourceBadge(job.source || 'youtube')}</span>
                </div>`;
            }).join('');

            container.querySelectorAll('.delete-file-btn').forEach((btn) => {
                btn.addEventListener('click', (event) => {
                    event.stopPropagation();
                    deleteJobFile(btn.dataset.jobId || '', btn.dataset.trackName || '');
                });
            });

            container.querySelectorAll('.report-btn').forEach((btn) => {
                btn.addEventListener('click', (event) => {
                    event.stopPropagation();
                    openReportDialog(
                        btn.dataset.jobId || '',
                        btn.dataset.videoId || '',
                        btn.dataset.uploader || '',
                        btn.dataset.source || 'youtube'
                    );
                });
            });

            container.querySelectorAll('.force-accept-btn').forEach((btn) => {
                btn.addEventListener('click', async (event) => {
                    event.stopPropagation();
                    btn.disabled = true;
                    btn.textContent = 'Queued...';
                    try {
                        const res = await apiFetch(`/api/jobs/${btn.dataset.jobId}/force-accept`, { method: 'POST' });
                        if (!res.ok) {
                            const err = await res.json().catch(() => ({}));
                            throw new Error(err.detail || 'Failed');
                        }
                        showToast('Re-queued with mismatch check disabled');
                        loadJobs();
                    } catch (e) {
                        showToast(`Force download failed: ${e.message}`, true);
                        btn.disabled = false;
                        btn.textContent = 'Force Download';
                    }
                });
            });

            container.querySelectorAll('.edit-tags-btn').forEach((btn) => {
                btn.addEventListener('click', (event) => {
                    event.stopPropagation();
                    openTagEditor({
                        jobId: btn.dataset.jobId || '',
                        artist: btn.dataset.artist || '',
                        title: btn.dataset.title || '',
                        album: btn.dataset.album || '',
                    });
                });
            });

            updateLiveQueueTimes();
        }

        function formatJobStatus(status) {
            if (status === 'completed_with_errors') {
                return 'completed (with errors)';
            }
            return status;
        }

        function formatMetadataSource(metadataSource) {
            const source = (metadataSource || '').toLowerCase();
            const labels = {
                'acoustid_fingerprint': 'AcoustID fingerprint',
                'musicbrainz_text': 'MusicBrainz text match',
                'youtube_guessed': 'YouTube embedded/guessed',
                'soundcloud_guessed': 'SoundCloud embedded/guessed',
                'mp3phoenix_guessed': 'MP3Phoenix embedded/guessed',
                'zvu4no_guessed': 'zvu4no embedded/guessed',
                'freemp3cloud_guessed': 'FreeMp3Cloud embedded/guessed',
                'soulseek_guessed': 'Soulseek embedded/guessed',
            };
            return labels[source] || metadataSource;
        }

        function formatDateYmd(isoString) {
            if (!isoString) return '';
            const normalized = /Z$|[+-]\d{2}:\d{2}$/.test(isoString) ? isoString : `${isoString.replace(' ', 'T')}Z`;
            const date = new Date(normalized);
            if (Number.isNaN(date.getTime())) return '';
            const y = date.getFullYear();
            const m = String(date.getMonth() + 1).padStart(2, '0');
            const d = String(date.getDate()).padStart(2, '0');
            return `${y}-${m}-${d}`;
        }

        function formatTime(isoString) {
            return formatDateYmd(isoString);
        }

        function toggleJobDetails(el) {
            const details = el.querySelector('.job-details');
            if (details) {
                const isOpen = details.style.display !== 'none';
                details.style.display = isOpen ? 'none' : 'block';
                el.classList.toggle('expanded', !isOpen);
                const jobId = el.dataset.jobId;
                if (jobId) {
                    if (isOpen) expandedJobIds.delete(jobId);
                    else expandedJobIds.add(jobId);
                }
            }
        }

        // Follows a "later succeeded on retry" pointer from an old failed card to
        // the job that actually finished the download. Retries always land on a
        // new Queue row, so this may be on a different page or sorted elsewhere.
        function jumpToJob(jobId) {
            const index = getSortedQueueJobs().findIndex(j => String(j.id) === String(jobId));
            if (index === -1) {
                showToast('That entry has dropped out of the loaded queue history', true);
                return;
            }
            queuePage = Math.floor(index / QUEUE_PAGE_SIZE) + 1;
            expandedJobIds.add(String(jobId));
            renderQueuePage();
            requestAnimationFrame(() => {
                const el = queueTab.querySelector(`.job-item[data-job-id="${jobId}"]`);
                if (!el) return;
                el.scrollIntoView({ behavior: 'smooth', block: 'center' });
                el.classList.add('job-jump-highlight');
                setTimeout(() => el.classList.remove('job-jump-highlight'), 1600);
            });
        }

        // =============================================================================
        // Score Rationale (why the scorer picked a particular result)
        // =============================================================================

        const SCORE_LABELS = {
            "base": null,
            "official_title": "Official title tag",
            "official_channel": "Official channel",
            "official_video": "Official music video",
            "official_audio": "Official audio",
            "topic_channel": "Topic channel",
            "live_requested": "Live version (requested)",
            "live_variant": "Live/session penalty",
            "live_channel": "Live channel penalty",
            "junk_variant": "Junk variant (karaoke/nightcore/etc.)",
            "bootleg_edit": "Bootleg/fan edit",
            "cover_or_variant": "Cover/remix/instrumental",
            "lyric_video": "Lyric video",
            "unofficial_title": "Unofficial/fan upload",
            "unofficial_channel": "Fan/tribute channel",
            "copyright_dodge": "Copyright-dodging upload",
            "channel_in_title": "Channel name in title",
            "query_full_coverage": "All search terms matched",
            "query_good_coverage": "Most search terms matched",
            "query_poor_coverage": "Few search terms matched",
            "title_match": "Exact title match",
            "title_overlap_full": "Strong title overlap",
            "title_overlap_strong": "Good title overlap",
            "title_mismatch": "Title mismatch",
            "artist_in_title": "Artist in title",
            "artist_in_channel": "Artist matches channel",
            "artist_match": "Artist matched",
            "artist_overlap": "Partial artist match",
            "artist_mismatch": "Artist mismatch",
            "artist_title_phrase": "Artist + title phrase match",
            "plain_title": "Plain studio title",
            "variant_suffix": "Variant suffix penalty",
            "reaction_or_compilation": "Reaction/compilation",
            "compilation_album": "Compilation album",
            "extended_version": "Extended version",
            "non_song_result": "Non-song result",
            "reverb": "Reverb edit",
            "duration_too_short": "Way too short",
            "duration_short": "Very short",
            "duration_brief": "Slightly short",
            "duration_sweet_spot": "Good duration",
            "duration_long": "Suspiciously long",
            "duration_very_long": "Way too long",
            "low_views": "Low views",
            "good_views": "Decent view count",
            "huge_views": "Very popular",
            "mb_duration_exact": "MusicBrainz duration match",
            "mb_duration_close": "Close to MusicBrainz duration",
            "mb_duration_off": "Duration doesn't match MusicBrainz",
            "mb_duration_way_off": "Duration way off MusicBrainz",
            "mb_search_duration": "MusicBrainz duration scoring",
            "source_quality": "Source quality bonus",
            "free_slot": "Free download slot",
            "fast_uploader": "Fast uploader",
            "popularity": "Popularity bonus",
        };

        function parseBreakdownEntry(entry) {
            // "live_variant=-180" → { key: "live_variant", delta: -180, label: "Live/session penalty" }
            const m = (entry || '').match(/^([a-z_]+)=([+-]?\d+)$/);
            if (!m) return null;
            const key = m[1];
            if (SCORE_LABELS[key] === null) return null;  // hidden (e.g. "base")
            return {
                key,
                delta: parseInt(m[2], 10),
                label: SCORE_LABELS[key] || key.replace(/_/g, ' '),
            };
        }

        function humaniseBreakdown(breakdown, topN) {
            const parsed = (breakdown || [])
                .map(parseBreakdownEntry)
                .filter(Boolean)
                .sort((a, b) => Math.abs(b.delta) - Math.abs(a.delta));
            const items = topN ? parsed.slice(0, topN) : parsed;
            return items.map(p => {
                const cls = p.delta >= 0 ? 'score-reason-positive' : 'score-reason-negative';
                const sign = p.delta >= 0 ? '+' : '';
                return `<span class="${cls}">${escapeHtml(p.label)} (${sign}${p.delta})</span>`;
            }).join(', ');
        }

        function renderRationalePanel(data) {
            const d = data.decision || {};
            const selected = d.selected;
            const runnersUp = d.runners_up || [];
            if (!selected) return '';

            let html = '<div class="score-rationale">';
            html += '<div class="score-rationale-header">';
            if (data.query) {
                html += `<span class="score-rationale-query">Search: ${escapeHtml(data.query)}</span>`;
            }
            html += '</div>';

            // Winner
            html += '<div class="score-rationale-candidate score-rationale-picked">';
            html += '<div class="score-rationale-candidate-header">';
            html += `<span class="score-rationale-label">Picked</span>`;
            html += `<span class="source-badge ${escapeHtml(selected.source || 'youtube')}" style="font-size:10px;padding:1px 6px;">${getSourceBadge(selected.source || 'youtube')}</span>`;
            html += `<span class="score-rationale-title">${escapeHtml(selected.channel || '')}${selected.channel ? ' \u2013 ' : ''}${escapeHtml(selected.title || '')}</span>`;
            html += `<span class="score-rationale-score">score ${selected.score != null ? selected.score : '?'}</span>`;
            html += '</div>';
            const selectedReasons = humaniseBreakdown(selected.breakdown, 5);
            if (selectedReasons) {
                html += `<div class="score-rationale-reasons">${selectedReasons}</div>`;
            }
            html += '</div>';

            // Runners-up
            runnersUp.forEach((ru, i) => {
                html += '<div class="score-rationale-candidate score-rationale-runnerup">';
                html += '<div class="score-rationale-candidate-header">';
                html += `<span class="score-rationale-label">#${i + 2}</span>`;
                html += `<span class="source-badge ${escapeHtml(ru.source || 'youtube')}" style="font-size:10px;padding:1px 6px;">${getSourceBadge(ru.source || 'youtube')}</span>`;
                html += `<span class="score-rationale-title">${escapeHtml(ru.channel || '')}${ru.channel ? ' \u2013 ' : ''}${escapeHtml(ru.title || '')}</span>`;
                html += `<span class="score-rationale-score">score ${ru.score != null ? ru.score : '?'}</span>`;
                html += '</div>';
                const ruReasons = humaniseBreakdown(ru.breakdown, 3);
                if (ruReasons) {
                    html += `<div class="score-rationale-reasons">${ruReasons}</div>`;
                }
                html += '</div>';
            });

            // Raw scoring expander
            const allBreakdowns = [];
            allBreakdowns.push(`Picked: ${(selected.breakdown || []).join(', ')}`);
            runnersUp.forEach((ru, i) => {
                allBreakdowns.push(`#${i + 2}: ${(ru.breakdown || []).join(', ')}`);
            });
            html += '<details class="score-rationale-raw">';
            html += '<summary>Raw scoring</summary>';
            html += `<pre>${escapeHtml(allBreakdowns.join('\n'))}</pre>`;
            html += '</details>';

            html += '</div>';
            return html;
        }

        async function loadScoreRationale(jobId) {
            const container = document.getElementById(`score-rationale-${jobId}`);
            const link = document.getElementById(`score-rationale-link-${jobId}`);
            if (!container) return;

            // Already loaded?
            if (container.dataset.loaded === 'true') {
                container.style.display = container.style.display === 'none' ? 'block' : 'none';
                return;
            }

            try {
                const res = await apiFetch(`/api/jobs/${jobId}/score-rationale`);
                if (!res.ok) {
                    // No data for this job; hide the link entirely
                    if (link) link.style.display = 'none';
                    return;
                }
                const data = await res.json();
                container.innerHTML = renderRationalePanel(data);
                container.dataset.loaded = 'true';
                container.style.display = 'block';
            } catch {
                if (link) link.style.display = 'none';
            }
        }

        function copyScoreRationale(jobId) {
            // Fetches fresh rather than relying on the "Why this result?" panel
            // having been opened first; Copy used to silently do nothing if you
            // reached for it before that click had populated the cache.
            apiFetch(`/api/jobs/${jobId}/score-rationale`)
                .then(res => res.ok ? res.json() : null)
                .then(data => {
                    if (!data) { showToast('No scoring rationale recorded for this job', true); return; }
                    const d = data.decision || {};
                    const sel = d.selected;
                    if (!sel) { showToast('No scoring rationale recorded for this job', true); return; }

                    let text = '';
                    if (data.query) text += `Search: ${data.query}\n`;
                    text += `\nPicked: [${sel.source}] ${sel.channel} - ${sel.title}\n`;
                    text += `Score: ${sel.score}\n`;
                    text += `Breakdown: ${(sel.breakdown || []).join(', ')}\n`;

                    const runners = d.runners_up || [];
                    if (runners.length) {
                        text += `\nRunners-up:\n`;
                        runners.forEach((ru, i) => {
                            text += `  #${i + 2}: [${ru.source}] ${ru.channel} - ${ru.title} (score ${ru.score})\n`;
                            text += `  Breakdown: ${(ru.breakdown || []).join(', ')}\n`;
                        });
                    }

                    navigator.clipboard.writeText(text.trim()).then(() => {
                        showToast('Score rationale copied');
                    });
                })
                .catch(() => showToast('Could not fetch scoring rationale', true));
        }

        function escapeHtmlText(value) {
            return escapeHtml(value == null ? '' : String(value));
        }

        function renderTagEditorPreview() {
            if (!activeTagEditor) return;
            const artist = (tagEditorArtist.value || '').trim();
            const title = (tagEditorTitle.value || '').trim();
            const album = (tagEditorAlbum.value || '').trim();
            const albumArtist = (tagEditorAlbumArtist.value || '').trim();
            const year = (tagEditorYear.value || '').trim();
            const trackNumber = (tagEditorTrackNumber.value || '').trim();
            const trackTotal = (tagEditorTrackTotal.value || '').trim();
            const filename = [artist, title].filter(Boolean).join(' - ') || 'Untitled track';
            tagEditorPreview.innerHTML = `
                <div><strong>Filename preview:</strong> ${escapeHtmlText(filename)}${escapeHtmlText(activeTagEditor.extension || '')}</div>
                <div><strong>Album tag:</strong> ${album ? escapeHtmlText(album) : '<em>empty</em>'}</div>
                <div><strong>Album artist:</strong> ${albumArtist ? escapeHtmlText(albumArtist) : '<em>empty</em>'}</div>
                <div><strong>Track:</strong> ${trackNumber ? `${escapeHtmlText(trackNumber)}${trackTotal ? ` / ${escapeHtmlText(trackTotal)}` : ''}` : '<em>empty</em>'}</div>
                <div><strong>Year:</strong> ${year ? escapeHtmlText(year) : '<em>empty</em>'}</div>
            `;
        }

        function resetTagEditorDraft() {
            if (!activeTagEditor) return;
            tagEditorArtist.value = activeTagEditor.original.artist || '';
            tagEditorTitle.value = activeTagEditor.original.title || '';
            tagEditorAlbum.value = activeTagEditor.original.album || '';
            tagEditorAlbumArtist.value = activeTagEditor.original.album_artist || '';
            tagEditorYear.value = activeTagEditor.original.year || '';
            tagEditorTrackNumber.value = activeTagEditor.original.track_number || '';
            tagEditorTrackTotal.value = activeTagEditor.original.track_total || '';
            tagEditorError.style.display = 'none';
            tagEditorError.textContent = '';
            renderTagEditorPreview();
        }

        function openTagEditor({ jobId, artist, title, album, albumArtist = '', year = '', trackNumber = '', trackTotal = '' }) {
            const job = allQueueJobs.find(j => String(j.id) === String(jobId));
            activeTagEditor = {
                jobId: String(jobId || ''),
                extension: job?.file_path ? (job.file_path.match(/\.[^.\/\\]+$/)?.[0] || '') : '',
                sourceLabel: getSourceLabel(job?.source || 'youtube'),
                guessArtistSeed: (artist || '').trim(),
                guessTitleSeed: (title || '').trim(),
                guessOffset: 0,
                original: {
                    artist: artist || '',
                    title: title || '',
                    album: album || '',
                    album_artist: albumArtist || '',
                    year: year || '',
                    track_number: trackNumber || '',
                    track_total: trackTotal || '',
                },
            };
            tagEditorMeta.textContent = [activeTagEditor.sourceLabel, activeTagEditor.jobId].filter(Boolean).join(' • ');
            resetTagEditorDraft();
            tagEditorOverlay.style.display = 'flex';
            setTimeout(() => tagEditorArtist.focus(), 0);
        }

        function closeTagEditor() {
            activeTagEditor = null;
            tagEditorOverlay.style.display = 'none';
            tagEditorError.style.display = 'none';
            tagEditorError.textContent = '';
            tagEditorGuessBtn.disabled = false;
            tagEditorGuessBtn.textContent = 'Guess from MusicBrainz';
            tagEditorSaveBtn.disabled = false;
            tagEditorSaveBtn.textContent = 'Save Tags';
        }

        async function saveTagEditor() {
            if (!activeTagEditor) return;

            const artist = (tagEditorArtist.value || '').trim();
            const title = (tagEditorTitle.value || '').trim();
            const album = (tagEditorAlbum.value || '').trim();
            const albumArtist = (tagEditorAlbumArtist.value || '').trim();
            const year = (tagEditorYear.value || '').trim();
            const rawTrackNumber = tagEditorTrackNumber.value ? parseInt(tagEditorTrackNumber.value, 10) : null;
            const rawTrackTotal = tagEditorTrackTotal.value ? parseInt(tagEditorTrackTotal.value, 10) : null;
            const trackNumber = Number.isFinite(rawTrackNumber) ? rawTrackNumber : null;
            const trackTotal = Number.isFinite(rawTrackTotal) ? rawTrackTotal : null;

            if (!artist || !title) {
                tagEditorError.textContent = 'Artist and title are required.';
                tagEditorError.style.display = 'block';
                return;
            }
            if (year && !/^\d{4}$/.test(year)) {
                tagEditorError.textContent = 'Year must be four digits.';
                tagEditorError.style.display = 'block';
                return;
            }
            if (trackNumber && trackTotal && trackNumber > trackTotal) {
                tagEditorError.textContent = 'Track number cannot be greater than total tracks.';
                tagEditorError.style.display = 'block';
                return;
            }

            const originalText = tagEditorSaveBtn.textContent;
            tagEditorSaveBtn.disabled = true;
            tagEditorSaveBtn.textContent = 'Saving...';
            tagEditorError.style.display = 'none';

            try {
                const res = await apiFetch(`/api/jobs/${activeTagEditor.jobId}/tags`, {
                    method: 'PATCH',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        artist,
                        title,
                        album,
                        album_artist: albumArtist,
                        year,
                        track_number: trackNumber,
                        track_total: trackTotal,
                    }),
                });
                if (!res.ok) {
                    const err = await res.json().catch(() => ({}));
                    throw new Error(err.detail || 'Failed to save tags');
                }

                const card = queueTab.querySelector(`.job-item[data-job-id="${activeTagEditor.jobId}"]`);
                if (card) {
                    const titleEl = card.querySelector('.job-title');
                    if (titleEl) titleEl.firstChild.textContent = artist ? `${artist} - ${title}` : title;
                    const editBtn = card.querySelector('.edit-tags-btn');
                    if (editBtn) {
                        editBtn.dataset.artist = artist;
                        editBtn.dataset.title = title;
                        editBtn.dataset.album = album;
                    }
                }
                const queueJob = allQueueJobs.find(j => String(j.id) === String(activeTagEditor.jobId));
                if (queueJob) {
                    queueJob.artist = artist;
                    queueJob.title = title;
                    queueJob.album_name = album;
                }

                showToast('Tags updated');
                closeTagEditor();
            } catch (e) {
                tagEditorError.textContent = e.message || 'Failed to save tags.';
                tagEditorError.style.display = 'block';
                tagEditorSaveBtn.disabled = false;
                tagEditorSaveBtn.textContent = originalText;
            }
        }

        async function guessTagEditorFromMusicBrainz() {
            if (!activeTagEditor) return;
            const artist = (tagEditorArtist.value || '').trim();
            const title = (tagEditorTitle.value || '').trim();
            if (!artist || !title) {
                tagEditorError.textContent = 'Enter artist and title before guessing.';
                tagEditorError.style.display = 'block';
                return;
            }

            const originalText = tagEditorGuessBtn.textContent;
            tagEditorGuessBtn.disabled = true;
            tagEditorGuessBtn.textContent = 'Guessing...';
            tagEditorError.style.display = 'none';

            try {
                if (artist !== activeTagEditor.guessArtistSeed || title !== activeTagEditor.guessTitleSeed) {
                    activeTagEditor.guessArtistSeed = artist;
                    activeTagEditor.guessTitleSeed = title;
                    activeTagEditor.guessOffset = 0;
                }
                const res = await apiFetch(`/api/jobs/${activeTagEditor.jobId}/musicbrainz-guess?artist=${encodeURIComponent(artist)}&title=${encodeURIComponent(title)}&offset=${activeTagEditor.guessOffset}`);
                if (!res.ok) {
                    const err = await res.json().catch(() => ({}));
                    throw new Error(err.detail || 'No more suitable MusicBrainz matches found');
                }
                const guess = await res.json();
                tagEditorArtist.value = guess.artist || artist;
                tagEditorTitle.value = guess.title || title;
                tagEditorAlbum.value = guess.album || '';
                tagEditorAlbumArtist.value = guess.album_artist || '';
                tagEditorYear.value = guess.year || '';
                tagEditorTrackNumber.value = guess.track_number || '';
                tagEditorTrackTotal.value = guess.track_total || '';
                activeTagEditor.guessOffset = (guess.candidate_index || 0) + 1;
                renderTagEditorPreview();
                if ((guess.candidate_count || 0) > 1) {
                    tagEditorGuessBtn.textContent = activeTagEditor.guessOffset < guess.candidate_count ? 'Guess Again' : 'No More Guesses';
                } else {
                    tagEditorGuessBtn.textContent = 'Guess Again';
                }
                showToast(`MusicBrainz metadata applied${guess.candidate_count ? ` (${(guess.candidate_index || 0) + 1}/${guess.candidate_count})` : ''}`);
            } catch (e) {
                tagEditorError.textContent = e.message || 'MusicBrainz lookup failed.';
                tagEditorError.style.display = 'block';
                tagEditorGuessBtn.textContent = 'Guess Again';
            } finally {
                tagEditorGuessBtn.disabled = false;
                if (tagEditorGuessBtn.textContent === 'Guessing...') {
                    tagEditorGuessBtn.textContent = originalText;
                }
            }
        }

        async function redownloadJob(jobId) {
            try {
                showToast('Re-downloading...');
                const response = await apiFetch(`/api/jobs/${jobId}/retry`, { method: 'POST' });
                if (!response.ok) {
                    const data = await response.json();
                    throw new Error(data.detail || 'Re-download failed');
                }
                showToast('Queued for re-download');
                loadJobs();
            } catch (error) {
                showToast(error.message || 'Re-download failed', true);
            }
        }

        async function retryNextJob(jobId) {
            try {
                showToast('Finding the next allowed option...');
                const response = await apiFetch(`/api/jobs/${jobId}/retry-next`, { method: 'POST' });
                if (!response.ok) {
                    const data = await response.json().catch(() => ({}));
                    throw new Error(data.detail || 'Retry failed');
                }
                showToast('Queued a new rescue attempt');
                loadJobs();
            } catch (error) {
                showToast(error.message || 'Retry failed', true);
            }
        }

        async function deleteJobFile(jobId, trackName) {
            if (!confirm(`Move "${trackName}" to trash?`)) return;

            try {
                const response = await apiFetch(`/api/jobs/${jobId}/file`, { method: 'DELETE' });
                if (!response.ok) {
                    const data = await response.json();
                    throw new Error(data.detail || 'Delete failed');
                }
                const data = await response.json();
                if (data.trashed) {
                    showToast(`Moved to trash (${data.deleted.length} file(s))`);
                } else if (data.deleted.length > 0) {
                    showToast(`Deleted ${data.deleted.length} file(s)`);
                } else {
                    showToast('File was already missing, marked as deleted');
                }
                loadJobs();
                loadTrash();
            } catch (error) {
                showToast(error.message || 'Delete failed', true);
                loadJobs();
            }
        }

        // =============================================================================
        // Trash Bin
        // =============================================================================

        async function loadTrash() {
            const section = document.getElementById('trashSection');
            const list = document.getElementById('trashList');
            try {
                const response = await apiFetch('/api/trash');
                const data = await response.json();
                if (!response.ok) {
                    throw new Error(data.detail || 'Failed to load trash');
                }

                if (!data.files.length) {
                    section.style.display = 'none';
                    list.innerHTML = '';
                    return;
                }

                section.style.display = 'block';
                const sizeLabel = formatFileSize(data.total_size);

                list.innerHTML = `
                    <div style="margin-bottom: 8px; font-size: 12px; color: var(--text-muted);">
                        ${data.files.length} file(s), ${sizeLabel} total
                    </div>
                    ${data.files.map(f => `
                        <div class="job-item" style="cursor: default;">
                            <div class="job-status completed"></div>
                            <div class="job-info">
                                <div class="job-title">${escapeHtml(f.name)}</div>
                                <div class="job-meta">${escapeHtml(f.path)} &middot; ${formatFileSize(f.size)}</div>
                            </div>
                            <div style="display: flex; gap: 6px; align-items: center; flex-shrink: 0;">
                                <button class="library-play-btn" aria-label="Play or pause" title="Play or pause" onclick="playTrashAudio('${escapeAttr(f.path)}', this)" style="padding: 6px 12px; font-size: 11px; font-family: inherit; font-weight: 600; background: var(--bg-tertiary); color: var(--accent); border: 1px solid var(--border); border-radius: 6px; cursor: pointer;"><i class="fa-solid fa-play"></i></button>
                                <button onclick="restoreTrashFile('${escapeAttr(f.path)}')" style="padding: 6px 12px; font-size: 11px; font-family: inherit; font-weight: 600; background: var(--bg-tertiary); color: var(--accent); border: 1px solid var(--border); border-radius: 6px; cursor: pointer;">Restore</button>
                                <button onclick="deleteTrashFile('${escapeAttr(f.path)}', '${escapeAttr(f.name)}')" style="padding: 6px 12px; font-size: 11px; font-family: inherit; font-weight: 600; background: var(--bg-tertiary); color: var(--error); border: 1px solid var(--border); border-radius: 6px; cursor: pointer;">Delete</button>
                            </div>
                        </div>
                    `).join('')}
                `;
            } catch (e) {
                console.error('Failed to load trash', e);
                section.style.display = 'block';
                list.innerHTML = `
                    <div class="job-item" style="cursor: default;">
                        <div class="job-info">
                            <div class="job-title">Trash unavailable</div>
                            <div class="job-meta">${escapeHtml(e?.message || 'Failed to load trash')}</div>
                        </div>
                    </div>
                `;
            }
        }

        function formatFileSize(bytes) {
            if (bytes < 1024) return bytes + ' B';
            if (bytes < 1048576) return (bytes / 1024).toFixed(1) + ' KB';
            if (bytes < 1073741824) return (bytes / 1048576).toFixed(1) + ' MB';
            return (bytes / 1073741824).toFixed(2) + ' GB';
        }

        async function restoreTrashFile(path) {
            try {
                const response = await apiFetch(`/api/trash/restore?path=${encodeURIComponent(path)}`, { method: 'POST' });
                if (!response.ok) {
                    const data = await response.json();
                    throw new Error(data.detail || 'Restore failed');
                }
                const data = await response.json();
                showToast(`Restored: ${data.restored}`);
                loadTrash();
            } catch (error) {
                showToast(error.message || 'Restore failed', true);
            }
        }

        async function deleteTrashFile(path, name) {
            if (!confirm(`Permanently delete "${name}"? This cannot be undone.`)) return;
            try {
                const response = await apiFetch(`/api/trash/file?path=${encodeURIComponent(path)}`, { method: 'DELETE' });
                if (!response.ok) {
                    const data = await response.json();
                    throw new Error(data.detail || 'Delete failed');
                }
                showToast('Permanently deleted');
                loadTrash();
            } catch (error) {
                showToast(error.message || 'Delete failed', true);
            }
        }

        async function emptyTrash() {
            if (!confirm('Permanently delete everything in the trash? This cannot be undone.')) return;
            try {
                const response = await apiFetch('/api/trash', { method: 'DELETE' });
                if (!response.ok) {
                    const data = await response.json();
                    throw new Error(data.detail || 'Failed to empty trash');
                }
                const data = await response.json();
                showToast(`Trash emptied (${data.deleted} file(s) removed)`);
                loadTrash();
            } catch (error) {
                showToast(error.message || 'Failed to empty trash', true);
            }
        }

        // =============================================================================
        // Library / Trash Playback
        // =============================================================================

        let _libraryPlayingId = null;
        let _streamToken = null; // { token, expiresAt }

        async function _getStreamToken() {
            // An <audio> element cannot bolt on an Authorization header, so playback
            // rides on a short-lived token in the URL instead. Cached until it is
            // nearly stale, since one token covers a whole listening session.
            if (_streamToken && Date.now() < _streamToken.expiresAt - 60000) {
                return _streamToken.token;
            }
            const response = await apiFetch('/api/auth/stream-token', { method: 'POST' });
            if (!response.ok) throw new Error('Could not authorise playback');
            const data = await response.json();
            // Single-user mode hands back an empty token and no expiry; nothing to
            // refresh there, so just don't ask again for a while.
            const ttl = data.expires_in ? data.expires_in * 1000 : 3600000;
            _streamToken = { token: data.token || '', expiresAt: Date.now() + ttl };
            return _streamToken.token;
        }

        async function _startLibraryStream(id, url, btn) {
            stopPreview();
            stopLibraryPlayback();
            _libraryPlayingId = id;
            if (btn) btn.innerHTML = '<i class="fa-solid fa-stop"></i>';

            let src;
            try {
                const token = await _getStreamToken();
                const separator = url.includes('?') ? '&' : '?';
                src = withRootPath(token ? `${url}${separator}stream_token=${encodeURIComponent(token)}` : url);
            } catch (error) {
                stopLibraryPlayback();
                showToast('Could not play file', true);
                return;
            }
            // They may have hit stop, or started something else, while we were away.
            if (_libraryPlayingId !== id) return;

            previewAudio.oncanplay = () => {
                previewAudio.oncanplay = null;
                previewAudio.play().catch(() => {});
            };
            previewAudio.onended = () => stopLibraryPlayback();
            previewAudio.onerror = (e) => {
                // Ignore aborted loads (caused by stopLibraryPlayback clearing src)
                if (previewAudio.error && previewAudio.error.code === MediaError.MEDIA_ERR_SRC_NOT_SUPPORTED) {
                    // Could be an expired token rather than a duff file; bin it so the
                    // next attempt starts with a fresh one.
                    _streamToken = null;
                    stopLibraryPlayback();
                    showToast('Could not play file', true);
                }
            };
            previewAudio.volume = previewVolume;
            previewAudio.src = src;
            previewAudio.load();
        }

        function playJobAudio(jobId, btn) {
            if (_libraryPlayingId === jobId) { stopLibraryPlayback(); return; }
            _startLibraryStream(jobId, `/api/jobs/${jobId}/stream`, btn);
        }

        function playTrashAudio(path, btn) {
            if (_libraryPlayingId === 'trash:' + path) { stopLibraryPlayback(); return; }
            _startLibraryStream('trash:' + path, `/api/trash/stream?path=${encodeURIComponent(path)}`, btn);
        }

        function stopLibraryPlayback() {
            if (!_libraryPlayingId) return;
            previewAudio.oncanplay = null;
            previewAudio.onended = null;
            previewAudio.onerror = null;
            previewAudio.pause();
            previewAudio.src = '';
            // Reset all play buttons back to the play icon
            document.querySelectorAll('.library-play-btn').forEach(b => {
                b.innerHTML = '<i class="fa-solid fa-play"></i>';
            });
            _libraryPlayingId = null;
        }

        // =============================================================================
        // Report / Blacklist
        // =============================================================================

        function openReportDialog(jobId, videoId, uploader, source) {
            document.getElementById('reportJobId').value = jobId;
            document.getElementById('reportVideoId').value = videoId;
            document.getElementById('reportSource').value = source;
            document.getElementById('reportUploader').value = uploader;
            document.getElementById('reportReason').value = 'wrong_track';
            document.getElementById('reportNote').value = '';
            document.getElementById('reportBlockUploader').checked = false;

            const uploaderRow = document.getElementById('reportUploaderRow');
            const uploaderName = document.getElementById('reportUploaderName');
            if (uploader) {
                uploaderName.textContent = uploader;
                uploaderRow.style.display = 'block';
            } else {
                uploaderRow.style.display = 'none';
            }

            const overlay = document.getElementById('reportOverlay');
            overlay.style.display = 'flex';
        }

        function closeReportDialog() {
            document.getElementById('reportOverlay').style.display = 'none';
        }

        tagEditorOverlay.addEventListener('click', (e) => {
            if (e.target === e.currentTarget) closeTagEditor();
        });
        tagEditorGuessBtn.addEventListener('click', guessTagEditorFromMusicBrainz);
        tagEditorResetBtn.addEventListener('click', resetTagEditorDraft);
        tagEditorSaveBtn.addEventListener('click', saveTagEditor);
        [tagEditorArtist, tagEditorTitle, tagEditorAlbum, tagEditorAlbumArtist, tagEditorYear, tagEditorTrackNumber, tagEditorTrackTotal].forEach((input) => {
            input.addEventListener('input', () => {
                if (tagEditorError.style.display !== 'none') {
                    tagEditorError.style.display = 'none';
                    tagEditorError.textContent = '';
                }
                renderTagEditorPreview();
            });
            input.addEventListener('keydown', (event) => {
                if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) {
                    event.preventDefault();
                    saveTagEditor();
                } else if (event.key === 'Escape') {
                    event.preventDefault();
                    closeTagEditor();
                }
            });
        });

        // Close on overlay click (not the dialog itself)
        document.getElementById('reportOverlay').addEventListener('click', (e) => {
            if (e.target === e.currentTarget) closeReportDialog();
        });
        document.getElementById('missingTrackVersionsOverlay').addEventListener('click', (e) => {
            if (e.target === e.currentTarget) closeMissingTrackVersionsModal();
        });
        document.getElementById('missingTrackVersionsRefreshBtn').addEventListener('click', async () => {
            if (missingTrackVersionsState.mode !== 'job') {
                loadMissingTrackVersions();
                return;
            }
            const body = document.getElementById('missingTrackVersionsBody');
            if (body) body.innerHTML = '<div class="missing-track-modal-empty">Searching allowed sources...</div>';
            try {
                const resp = await apiFetch(`/api/jobs/${encodeURIComponent(missingTrackVersionsState.jobId)}/rescue-candidates/refresh`, { method: 'POST' });
                if (!resp.ok) throw new Error('Search failed');
                const data = await resp.json();
                const results = Array.isArray(data.candidates) ? data.candidates : [];
                missingTrackVersionsState.results = results;
                if (!body) return;
                body.innerHTML = results.length
                    ? results.map((result, index) => _renderMissingTrackCandidate(result, index)).join('')
                    : '<div class="missing-track-modal-empty">No suitable alternatives found on the allowed sources.</div>';
                body.querySelectorAll('.missing-track-candidate').forEach((wrapper, index) => {
                    _attachMissingTrackCandidateHandlers(wrapper, results[index], index);
                });
            } catch (error) {
                if (body) body.innerHTML = '<div class="missing-track-modal-empty">Could not refresh alternatives right now.</div>';
            }
        });

        async function submitReport() {
            const btn = document.getElementById('reportSubmitBtn');
            btn.disabled = true;
            btn.textContent = 'Reporting...';

            try {
                const payload = {
                    job_id: document.getElementById('reportJobId').value || null,
                    video_id: document.getElementById('reportVideoId').value || null,
                    uploader: document.getElementById('reportUploader').value || null,
                    source: document.getElementById('reportSource').value || 'youtube',
                    reason: document.getElementById('reportReason').value,
                    note: document.getElementById('reportNote').value || null,
                    block_uploader: document.getElementById('reportBlockUploader').checked
                };

                const response = await apiFetch('/api/blacklist', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload)
                });

                if (!response.ok) {
                    const data = await response.json();
                    throw new Error(data.detail || 'Report failed');
                }

                showToast('Track reported and blacklisted');
                closeReportDialog();
            } catch (error) {
                showToast(error.message || 'Report failed', true);
            } finally {
                btn.disabled = false;
                btn.textContent = 'Report';
            }
        }

        const _reasonLabels = {
            wrong_track: 'Wrong track',
            poor_quality: 'Poor quality',
            slowed_pitched: 'Slowed/pitched',
            contentid: 'ContentID dodge',
            other: 'Other'
        };

        async function loadBlacklist() {
            const container = document.getElementById('blacklistContent');
            if (!container) return;

            try {
                const response = await apiFetch('/api/blacklist?limit=200');
                if (!response.ok) throw new Error('Failed to load blacklist');

                const data = await response.json();
                const entries = data.entries || [];

                if (entries.length === 0) {
                    container.innerHTML = '<p style="color:var(--text-secondary); font-style:italic;">No blacklisted items yet. Use the Report button on queue items to flag bad tracks.</p>';
                    return;
                }

                container.innerHTML = `
                    <table style="width:100%; border-collapse:collapse; font-size:12px;">
                        <thead>
                            <tr style="text-align:left; color:var(--text-secondary); border-bottom:1px solid var(--border);">
                                <th style="padding:6px 8px;">Type</th>
                                <th style="padding:6px 8px;">Value</th>
                                <th style="padding:6px 8px;">Source</th>
                                <th style="padding:6px 8px;">Reason</th>
                                <th style="padding:6px 8px;">Date</th>
                                <th style="padding:6px 8px;"></th>
                            </tr>
                        </thead>
                        <tbody>
                            ${entries.map(e => {
                                const isUploader = !e.video_id && e.uploader;
                                const typeLabel = isUploader ? 'Uploader' : 'Video';
                                const value = isUploader ? e.uploader : (e.video_id || '?');
                                const reasonLabel = _reasonLabels[e.reason] || e.reason || '';
                                const dateStr = formatDateYmd(e.created_at);
                                return `<tr style="border-bottom:1px solid var(--border);">
                                    <td style="padding:6px 8px;"><span style="display:inline-block; padding:2px 6px; font-size:10px; font-weight:600; border-radius:4px; background:${isUploader ? 'var(--warning)' : 'var(--error)'}; color:#000;">${typeLabel}</span></td>
                                    <td style="padding:6px 8px; max-width:160px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;" title="${escapeHtml(value)}">${escapeHtml(value)}</td>
                                    <td style="padding:6px 8px;">${escapeHtml(e.source || '')}</td>
                                    <td style="padding:6px 8px;">${escapeHtml(reasonLabel)}</td>
                                    <td style="padding:6px 8px;">${dateStr}</td>
                                    <td style="padding:6px 8px;"><button onclick="removeBlacklistEntry(${e.id})" style="padding:3px 8px; font-size:11px; font-family:inherit; font-weight:600; background:var(--bg-tertiary); color:var(--error); border:1px solid var(--border); border-radius:4px; cursor:pointer;">Remove</button></td>
                                </tr>`;
                            }).join('')}
                        </tbody>
                    </table>
                    <p style="margin-top:8px; color:var(--text-secondary); font-size:11px;">${entries.length} entr${entries.length === 1 ? 'y' : 'ies'}</p>
                `;
            } catch (error) {
                container.innerHTML = '<p style="color:var(--error);">Failed to load blacklist</p>';
            }
        }

        async function removeBlacklistEntry(entryId) {
            try {
                const response = await apiFetch(`/api/blacklist/${entryId}`, { method: 'DELETE' });
                if (!response.ok) {
                    const data = await response.json();
                    throw new Error(data.detail || 'Remove failed');
                }
                showToast('Blacklist entry removed');
                loadBlacklist();
            } catch (error) {
                showToast(error.message || 'Remove failed', true);
            }
        }

        function formatTimeFull(isoString) {
            return formatDateYmd(isoString);
        }

        function formatDuration(startIso, endIso) {
            if (!startIso || !endIso) return '';
            const start = new Date(startIso);
            const end = new Date(endIso);
            const diffMs = end - start;
            if (diffMs < 0) return '';
            const secs = Math.floor(diffMs / 1000);
            if (secs < 60) return `${secs}s`;
            const mins = Math.floor(secs / 60);
            const remainSecs = secs % 60;
            if (mins < 60) return `${mins}m ${remainSecs}s`;
            const hrs = Math.floor(mins / 60);
            const remainMins = mins % 60;
            return `${hrs}h ${remainMins}m`;
        }

        let toastHideTimer = null;

        function showToast(message, isError = false) {
            if (toastHideTimer) clearTimeout(toastHideTimer);
            toast.textContent = message;
            toast.className = 'toast' + (isError ? ' error' : '');
            toast.setAttribute('role', isError ? 'alert' : 'status');
            toast.setAttribute('aria-live', isError ? 'assertive' : 'polite');
            toast.classList.add('show');
            const readingTime = Math.min(6000, Math.max(2800, String(message || '').length * 55));
            toastHideTimer = setTimeout(() => {
                toast.classList.remove('show');
                toastHideTimer = null;
            }, readingTime);
        }

        function showVolumeMountWarning() {
            // Don't show if user dismissed it
            if (localStorage.getItem('dismissedVolumeWarning') === 'true') return;

            const banner = document.createElement('div');
            banner.className = 'warning-banner';
            banner.innerHTML = `
                <div class="warning-content">
                    <strong>Warning:</strong> Music directory doesn't appear to be mounted.
                    Downloads will be lost when the container restarts.
                    <a href="https://gitlab.com/g33kphr33k/musicgrabber#troubleshooting" target="_blank" rel="noopener">See setup guide</a>
                </div>
                <button class="warning-dismiss" onclick="dismissVolumeWarning(this.parentElement)">&times;</button>
            `;
            document.body.insertBefore(banner, document.body.firstChild);
        }

        function dismissVolumeWarning(banner) {
            localStorage.setItem('dismissedVolumeWarning', 'true');
            banner.remove();
        }

        async function bulkImport() {
            const songs = bulkInput.value.trim();
            if (!songs) {
                showToast('Please enter some songs', true);
                return;
            }

            // Validate playlist name if checkbox is checked
            const createPlaylist = createPlaylistCheckbox.checked;
            const playlistName = playlistNameInput.value.trim();

            if (createPlaylist && !playlistName) {
                showToast('Please enter a playlist name', true);
                playlistNameInput.focus();
                return;
            }

            bulkImportBtn.disabled = true;
            bulkImportBtn.textContent = 'Starting...';
            bulkResults.innerHTML = '<div class="loading"><div class="spinner"></div></div>';

            try {
                const requestBody = {
                    songs,
                    convert_audio: convertAudioCheckbox.checked
                };
                if (createPlaylist) {
                    requestBody.create_playlist = true;
                    requestBody.playlist_name = playlistName;
                    if (playlistNameInput.dataset.sourceUrl) {
                        requestBody.playlist_source_url = playlistNameInput.dataset.sourceUrl;
                    }
                    const usePlaylistsDirCheckbox = document.getElementById('usePlaylistsDirCheckbox');
                    if ((usePlaylistsDirCheckbox && usePlaylistsDirCheckbox.checked) || (!usePlaylistsDirCheckbox && serverConfig.playlists_subdir)) {
                        requestBody.use_playlists_dir = true;
                    }
                }
                if (bulkInput.dataset.originalYoutubeVideoMap) {
                    const originalRows = JSON.parse(bulkInput.dataset.originalYoutubeVideoMap);
                    const idsByTrack = new Map(originalRows.map(row => [row.track, row.video_id]));
                    requestBody.original_youtube_video_ids = songs.split('\n')
                        .map(line => idsByTrack.get(line.trim()) || null);
                }
                const bulkPriority = document.getElementById('bulkPrioritySource');
                if (bulkPriority && bulkPriority.value) {
                    requestBody.priority_source = bulkPriority.value;
                }

                // Start the async import
                const response = await apiFetch('/api/bulk-import-async', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(requestBody)
                });

                if (!response.ok) {
                    const error = await response.json();
                    throw new Error(error.detail || 'Bulk import failed');
                }

                const data = await response.json();
                currentBulkImportId = data.import_id;
                localStorage.setItem(userStorageKey('activeBulkImportId'), data.import_id);

                // Clear the textarea
                bulkInput.value = '';
                playlistNameInput.value = '';
                delete playlistNameInput.dataset.sourceUrl;
                delete bulkInput.dataset.originalYoutubeVideoMap;
                createPlaylistCheckbox.checked = false;
                playlistNameInput.style.display = 'none';
                const usePlaylistsDirCheckboxCleared = document.getElementById('usePlaylistsDirCheckbox');
                if (usePlaylistsDirCheckboxCleared) usePlaylistsDirCheckboxCleared.checked = false;
                const usePlaylistsDirRowCleared = document.getElementById('usePlaylistsDirRow');
                if (usePlaylistsDirRowCleared) usePlaylistsDirRowCleared.style.display = 'none';
                updateLineCounter();

                // Show initial progress
                showBulkImportProgress({
                    status: 'pending',
                    total_tracks: data.total_tracks,
                    searched: 0,
                    queued: 0,
                    failed: 0
                });

                // Start polling for progress
                startBulkImportPolling(data.import_id);

            } catch (error) {
                bulkResults.innerHTML = '';
                showToast(error.message || 'Bulk import failed', true);
                bulkImportBtn.disabled = false;
                bulkImportBtn.textContent = 'Import & Download All';
            }
        }

        function showBulkImportProgress(data) {
            const searchDone = ['completed', 'cancelled', 'error'].includes(data.status);
            // Show search progress while searching, download progress once searches are done
            const percent = data.total_tracks > 0
                ? (searchDone
                    ? Math.round(((data.completed + data.failed + (data.skipped || 0)) / data.total_tracks) * 100)
                    : Math.round((data.searched / data.total_tracks) * 100))
                : 0;
            const isComplete = data.complete;

            let statusText = 'Processing...';
            let statusColor = 'var(--accent)';
            if (data.rate_limited) {
                statusText = 'Rate limited - waiting...';
                statusColor = 'var(--warning)';
            } else if (data.status === 'cancelling') {
                statusText = 'Cancelling after active files...';
                statusColor = 'var(--warning)';
            } else if (data.status === 'cancelled') {
                statusText = 'Cancelled';
                statusColor = 'var(--text-secondary)';
            } else if (data.complete) {
                statusText = 'Complete';
                statusColor = 'var(--accent)';
            } else if (data.status === 'completed' && data.queued > 0) {
                statusText = `Downloading... (${data.queued} remaining)`;
                statusColor = 'var(--accent)';
            } else if (data.status === 'error') {
                statusText = 'Error';
                statusColor = 'var(--error)';
            }

            let html = `
                <div style="padding: 14px; background: var(--bg-secondary); border: 1px solid var(--border); border-radius: 12px; margin-bottom: 12px;">
                    <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px;">
                        <div style="font-size: 14px; font-weight: 600;">Import Progress</div>
                        <div style="display:flex;align-items:center;gap:8px;">
                            <div style="font-size: 12px; color: ${statusColor};">${statusText}</div>
                            ${!isComplete && data.status !== 'cancelling' ? '<button type="button" class="btn btn-ghost" style="padding:4px 8px;font-size:11px;" onclick="cancelBulkImport()">Cancel</button>' : ''}
                        </div>
                    </div>

                    <!-- Progress bar -->
                    <div style="background: var(--bg-tertiary); border-radius: 4px; height: 8px; margin-bottom: 12px; overflow: hidden;">
                        <div style="background: ${data.rate_limited ? 'var(--warning)' : 'var(--accent)'}; height: 100%; width: ${percent}%; transition: width 0.3s ease;"></div>
                    </div>

                    <div style="display: grid; grid-template-columns: repeat(5, 1fr); gap: 6px; text-align: center;">
                        <div>
                            <div style="font-size: 16px; font-weight: 600;">${data.searched}</div>
                            <div style="font-size: 10px; color: var(--text-secondary);">Searched</div>
                        </div>
                        <div>
                            <div style="font-size: 16px; font-weight: 600; color: var(--warning);">${data.queued}</div>
                            <div style="font-size: 10px; color: var(--text-secondary);">Queued</div>
                        </div>
                        <div>
                            <div style="font-size: 16px; font-weight: 600; color: var(--accent);">${data.completed || 0}</div>
                            <div style="font-size: 10px; color: var(--text-secondary);">Done</div>
                        </div>
                        <div>
                            <div style="font-size: 16px; font-weight: 600; color: var(--error);">${data.failed}</div>
                            <div style="font-size: 10px; color: var(--text-secondary);">Failed</div>
                        </div>
                        <div>
                            <div style="font-size: 16px; font-weight: 600;">${data.total_tracks}</div>
                            <div style="font-size: 10px; color: var(--text-secondary);">Total</div>
                        </div>
                    </div>
                </div>
            `;

            // Show error if any
            if (data.error) {
                html += `
                    <div style="padding: 14px; background: var(--bg-secondary); border: 1px solid var(--error); border-radius: 12px; margin-top: 12px;">
                        <div style="font-size: 13px; color: var(--error);">${escapeHtml(data.error)}</div>
                    </div>
                `;
            }

            bulkResults.innerHTML = html;

            // Update button state
            if (isComplete) {
                bulkImportBtn.disabled = false;
                bulkImportBtn.textContent = 'Import & Download All';
                if (data.status === 'completed') {
                    showToast(`Import complete: ${data.queued} queued, ${data.failed} failed`);
                } else if (data.status === 'cancelled') {
                    showToast('Import cancelled. Completed files and Queue history were kept.');
                }
            } else {
                bulkImportBtn.disabled = true;
                bulkImportBtn.textContent = `Processing ${data.searched}/${data.total_tracks}...`;
            }
        }

        async function cancelBulkImport() {
            if (!currentBulkImportId) return;
            try {
                const response = await apiFetch(`/api/bulk-import/${currentBulkImportId}/cancel`, {
                    method: 'POST'
                });
                if (!response.ok) {
                    const error = await response.json();
                    throw new Error(error.detail || 'Could not cancel import');
                }
                showToast('Cancellation requested. Up to two active files may finish.');
            } catch (error) {
                showToast(error.message || 'Could not cancel import', true);
            }
        }

        // Draws each bulk import track as the exact same job card the Queue tab
        // uses (same retry/report/edit-tags buttons, same click wiring), rather
        // than a second copy of that UI. A track that hasn't reached a job yet
        // (still being searched) has nothing for renderJobs to draw, so it gets
        // a plain placeholder row instead; it swaps for the real card the moment
        // a job_id shows up on the next poll.
        async function renderBulkImportJobList(importId, tracks) {
            if (!bulkImportJobsContainer) return;
            if (!tracks || !tracks.length) {
                bulkImportJobsContainer.innerHTML = '';
                return;
            }

            let jobsById = new Map();
            try {
                const resp = await apiFetch(`/api/jobs?import_id=${encodeURIComponent(importId)}`);
                if (resp.ok) {
                    const data = await resp.json();
                    jobsById = new Map((data.jobs || []).map(j => [String(j.id), j]));
                }
            } catch {
                // Transient; the next poll tick tries the fetch again.
            }

            const pending = tracks.filter(t => !t.job_id || !jobsById.has(String(t.job_id)));
            const jobsInOrder = tracks
                .filter(t => t.job_id && jobsById.has(String(t.job_id)))
                .map(t => jobsById.get(String(t.job_id)));

            const pendingLabels = { pending: 'Waiting to search…', searching: 'Searching…', cancelled: 'Cancelled' };
            const pendingHtml = pending.length ? `
                <div class="bulk-import-pending-list">
                    ${pending.map(track => `
                        <div class="job-item bulk-import-pending-row">
                            <div class="job-status ${track.status === 'failed' ? 'failed' : 'queued'}"></div>
                            <div class="job-info">
                                <div class="job-title">${escapeHtml(track.artist ? `${track.artist} - ${track.song}` : (track.song || ''))}</div>
                                <div class="job-meta"><span class="job-status-label">${escapeHtml(pendingLabels[track.status] || track.status || 'Waiting…')}</span></div>
                                ${track.error ? `<div class="job-error">${escapeHtml(track.error)}</div>` : ''}
                            </div>
                        </div>
                    `).join('')}
                </div>` : '';

            // Skip handing an empty array to renderJobs here: its empty state is
            // sized for a whole quiet Queue tab, not a gap under a live pending
            // list, and every track starts with no job_id anyway.
            bulkImportJobsContainer.innerHTML = jobsInOrder.length
                ? pendingHtml + '<div id="bulkImportJobCards"></div>'
                : pendingHtml;
            if (jobsInOrder.length) {
                renderJobs(jobsInOrder, document.getElementById('bulkImportJobCards'));
            }
        }

        function startBulkImportPolling(importId) {
            // Clear any existing polling
            if (bulkImportPollInterval) {
                clearInterval(bulkImportPollInterval);
            }

            // Poll every 2 seconds
            bulkImportPollInterval = setInterval(async () => {
                try {
                    const response = await apiFetch(`/api/bulk-import/${importId}/status`);
                    if (!response.ok) {
                        throw new Error('Failed to get status');
                    }

                    const data = await response.json();
                    showBulkImportProgress(data);
                    renderBulkImportJobList(importId, data.tracks);

                    // Stop polling when complete
                    if (data.complete) {
                        clearInterval(bulkImportPollInterval);
                        bulkImportPollInterval = null;
                        currentBulkImportId = null;
                        localStorage.removeItem(userStorageKey('activeBulkImportId'));
                    }
                } catch (error) {
                    console.error('Polling error:', error);
                    // Keep polling, might be a transient error
                }
            }, 2000);
        }

        // A reload used to lose track of an import that was still running in
        // the background entirely: the download carried on fine, the UI just
        // forgot it was ever watching anything. currentBulkImportId now has a
        // localStorage backup, so this picks the thread back up on load if
        // there's one to find, rather than leaving it to be discovered by
        // accident in the Queue tab.
        async function reattachBulkImportIfActive() {
            const storedId = localStorage.getItem(userStorageKey('activeBulkImportId'));
            if (!storedId) return;
            try {
                const response = await apiFetch(`/api/bulk-import/${storedId}/status`);
                if (!response.ok) {
                    localStorage.removeItem(userStorageKey('activeBulkImportId'));
                    return;
                }
                const data = await response.json();
                if (data.complete) {
                    localStorage.removeItem(userStorageKey('activeBulkImportId'));
                    return;
                }
                currentBulkImportId = storedId;
                showBulkImportProgress(data);
                renderBulkImportJobList(storedId, data.tracks);
                startBulkImportPolling(storedId);
            } catch {
                // Transient; leave the stored id alone and try again next load.
            }
        }

        // =============================================================
        // Shared album browser
        // =============================================================
        // Search artist -> pick artist -> list albums -> pick album ->
        // tracklist with existing/missing legend -> M3U toggle -> download ->
        // progress. Used to be the whole of the old Albums tab; now it is a
        // mountable component so the Artists tab (and, from Bulk Import
        // onwards, another agent's tab) can each get their own independent
        // instance without stepping on each other's state.
        //
        // mountAlbumBrowser(containerEl, options) -> handle
        //   options.artist            {mbid, name} to pre-select, skipping the
        //                              search step (used when pinning to a
        //                              followed artist)
        //   options.showArtistSearch  show the artist search box; false when
        //                              options.artist pins the browser to one
        //                              artist and searching elsewhere makes no
        //                              sense (default true)
        //   options.onQueued          callback(data) fired once
        //                              POST /api/albums/download succeeds
        //
        //   handle.setArtist(mbid, name)  programmatically select an artist
        //   handle.openAlbum(opts)        the Tracks-tab handoff entry point;
        //                                 opts: {artistName, albumTitle, artistMbid?, releaseMbid?}
        //   handle.hasSelection()         true if the user has already started
        //                                 a search or picked something, so
        //                                 callers know not to clobber it
        //   handle.searchFor(name)        fills the artist search box and runs
        //                                 the search (only when showArtistSearch)
        //   handle.reset()                back to a blank form (pinned artist
        //                                 mode returns to that artist's album list)
        //   handle.destroy()              stop polling and empty the container

        let _albumBrowserInstanceCounter = 0;

        function mountAlbumBrowser(containerEl, options = {}) {
            if (!containerEl) return null;
            const {
                artist = null,
                showArtistSearch = true,
                onQueued = null,
            } = options;

            const uid = `ab${++_albumBrowserInstanceCounter}`;
            const eid = (name) => `${uid}-${name}`;

            containerEl.innerHTML = `
                ${showArtistSearch ? `
                <div class="search-box">
                    <input type="text" id="${eid('artistInput')}" class="search-input" placeholder="Search for an artist…" autocomplete="off">
                    <button id="${eid('artistSearchBtn')}" class="btn btn-primary search-btn">Search</button>
                </div>
                <div class="album-search-note">
                    Album mode uses best-effort single-track matching, not full release mirroring. It will try to source each track intelligently, but it may not produce a perfect 1:1 album copy. For exact album hunting, use Lidarr (with Usenet/torrents) or Soulseek.
                </div>
                ` : ''}
                <div id="${eid('artistResults')}" class="album-artist-results" style="display:none;"></div>

                <div id="${eid('listSection')}" style="display:none;">
                    <h3 class="album-section-heading" id="${eid('listHeading')}"></h3>
                    <div id="${eid('list')}" class="album-list"></div>
                </div>

                <div id="${eid('tracklistSection')}" style="display:none;">
                    <h3 class="album-section-heading" id="${eid('tracklistHeading')}"></h3>
                    <div id="${eid('existingWarning')}" class="album-existing-warning" style="display:none;"></div>
                    <div id="${eid('trackLegend')}" class="album-track-legend" style="display:none;"></div>
                    <ol id="${eid('tracklist')}" class="album-tracklist"></ol>
                    <div class="bulk-playlist-row">
                        <input type="checkbox" id="${eid('makeM3u')}">
                        <label for="${eid('makeM3u')}">Generate M3U playlist</label>
                    </div>
                    <div id="${eid('m3uHint')}" class="album-m3u-hint" style="display:none;"></div>
                    <button id="${eid('downloadBtn')}" class="btn btn-primary btn-lg btn-block bulk-import-btn album-download-btn">
                        Download Album
                    </button>
                    <button id="${eid('resetBtn')}" class="btn btn-ghost album-reset-btn" type="button">
                        ${showArtistSearch ? 'Search Another Album' : 'Pick Another Album'}
                    </button>
                </div>

                <div id="${eid('progress')}" class="album-download-progress" style="display:none;"></div>
            `;

            const els = {
                artistInput: document.getElementById(eid('artistInput')),
                artistSearchBtn: document.getElementById(eid('artistSearchBtn')),
                artistResults: document.getElementById(eid('artistResults')),
                listSection: document.getElementById(eid('listSection')),
                list: document.getElementById(eid('list')),
                listHeading: document.getElementById(eid('listHeading')),
                tracklistSection: document.getElementById(eid('tracklistSection')),
                tracklistHeading: document.getElementById(eid('tracklistHeading')),
                existingWarning: document.getElementById(eid('existingWarning')),
                trackLegend: document.getElementById(eid('trackLegend')),
                tracklist: document.getElementById(eid('tracklist')),
                makeM3u: document.getElementById(eid('makeM3u')),
                m3uHint: document.getElementById(eid('m3uHint')),
                downloadBtn: document.getElementById(eid('downloadBtn')),
                resetBtn: document.getElementById(eid('resetBtn')),
                progress: document.getElementById(eid('progress')),
            };

            const state = {
                selectedArtist: artist ? { mbid: artist.mbid, name: artist.name } : null,
                selectedRelease: null,
                selectedM3uName: null,
                pollInterval: null,
            };

            function setResetVisible(visible) {
                if (els.resetBtn) els.resetBtn.classList.toggle('show', !!visible);
            }

            async function _readApiErrorDetail(resp) {
                // FastAPI puts errors in {detail: "..."}. Fall back to the status line if
                // the body is empty / not JSON.
                try {
                    const data = await resp.json();
                    if (data && data.detail) return String(data.detail);
                } catch {}
                return `HTTP ${resp.status}`;
            }

            function _renderRetryError(node, message, onRetry, asListItem = false) {
                if (!node) return;
                const wrap = document.createElement(asListItem ? 'li' : 'p');
                if (!asListItem) wrap.className = 'bulk-intro-text error-text';
                else wrap.className = 'album-track-item album-track-error';
                wrap.textContent = message + ' ';
                const btn = document.createElement('button');
                btn.type = 'button';
                btn.className = 'album-retry-btn';
                btn.textContent = 'Retry';
                btn.addEventListener('click', () => { onRetry(); });
                wrap.appendChild(btn);
                node.innerHTML = '';
                node.appendChild(wrap);
            }

            function findMatchingAlbum(albums, searchTitle) {
                if (!searchTitle || !albums.length) return null;
                function normalise(s) {
                    return s.toLowerCase()
                        .replace(/\s*\(.*?\)\s*/g, ' ')
                        .replace(/\s*\[.*?\]\s*/g, ' ')
                        .replace(/[^\w\s]/g, '')
                        .replace(/\s+/g, ' ')
                        .trim();
                }
                const norm = normalise(searchTitle);
                for (const a of albums) if (a.title.toLowerCase() === searchTitle.toLowerCase()) return a;
                for (const a of albums) if (normalise(a.title) === norm) return a;
                for (const a of albums) {
                    const na = normalise(a.title);
                    if (na && norm && (na.includes(norm) || norm.includes(na))) return a;
                }
                return null;
            }

            async function searchArtist() {
                const q = els.artistInput ? els.artistInput.value.trim() : '';
                if (!q || !els.artistResults) return;

                els.artistResults.innerHTML = `<p class="bulk-intro-text">${escapeHtml(_musicbrainzQuip())}…</p>`;
                els.artistResults.style.display = '';
                if (els.listSection) els.listSection.style.display = 'none';
                if (els.tracklistSection) els.tracklistSection.style.display = 'none';
                if (els.progress) els.progress.style.display = 'none';
                setResetVisible(false);
                state.selectedArtist = null;
                state.selectedRelease = null;
                state.selectedM3uName = null;
                if (state.pollInterval) { clearInterval(state.pollInterval); state.pollInterval = null; }
                if (els.downloadBtn) { els.downloadBtn.disabled = false; els.downloadBtn.textContent = 'Download Album'; }

                try {
                    const resp = await apiFetch(`/api/albums/search-artist?q=${encodeURIComponent(q)}`);
                    if (!resp.ok) {
                        const detail = await _readApiErrorDetail(resp);
                        _renderRetryError(els.artistResults, `Search failed: ${detail}`, searchArtist);
                        return;
                    }
                    const data = await resp.json();
                    const artists = data.artists || [];
                    if (!artists.length) {
                        els.artistResults.innerHTML = '<p class="bulk-intro-text">No artists found.</p>';
                        return;
                    }
                    els.artistResults.innerHTML = '';
                    for (const a of artists) {
                        const btn = document.createElement('button');
                        btn.className = 'album-artist-btn';
                        btn.innerHTML = `<span class="album-artist-name">${escapeHtml(a.name)}</span>`
                            + (a.disambiguation ? ` <span class="album-artist-disambig">${escapeHtml(a.disambiguation)}</span>` : '');
                        btn.addEventListener('click', () => selectArtist(a, btn));
                        els.artistResults.appendChild(btn);
                    }
                } catch (e) {
                    _renderRetryError(els.artistResults, `Search failed: ${e.message}`, searchArtist);
                }
            }

            async function loadAlbums(a) {
                try {
                    const resp = await apiFetch(`/api/albums/artist/${encodeURIComponent(a.mbid)}/albums`);
                    if (!resp.ok) {
                        const detail = await _readApiErrorDetail(resp);
                        _renderRetryError(els.list, `Failed to load albums: ${detail}`, () => loadAlbums(a));
                        return;
                    }
                    const data = await resp.json();
                    const albums = data.albums || [];
                    if (!els.list) return albums;
                    if (!albums.length) {
                        els.list.innerHTML = '<p class="bulk-intro-text">No albums found.</p>';
                        return albums;
                    }
                    els.list.innerHTML = '';
                    const artworkTargets = [];
                    for (const album of albums) {
                        const albumBtn = document.createElement('button');
                        albumBtn.className = 'album-list-btn';
                        // EPs sit in this list alongside albums, so say so; some
                        // artists are basically all EPs and an untagged list of
                        // six identical-looking entries helps nobody.
                        albumBtn.innerHTML = '<div class="album-list-thumb"></div>'
                            + `<span class="album-list-title">${escapeHtml(album.title)}</span>`
                            + (album.year ? ` <span class="album-list-year">${escapeHtml(album.year)}</span>` : '')
                            + (album.primary_type === 'EP' ? ' <span class="album-list-year">(EP)</span>' : '');
                        albumBtn.addEventListener('click', () => selectAlbum(album, albumBtn));
                        els.list.appendChild(albumBtn);
                        artworkTargets.push({ thumbEl: albumBtn.querySelector('.album-list-thumb'), title: album.title });
                    }
                    // Text is on screen already; covers trickle in behind it so a
                    // slow iTunes/Deezer lookup never holds up the album list.
                    fetchAlbumListArtwork(artworkTargets, a.name);
                    return albums;
                } catch (e) {
                    _renderRetryError(els.list, `Failed to load albums: ${e.message}`, () => loadAlbums(a));
                }
            }

            async function selectArtist(a, btn) {
                state.selectedArtist = a;
                state.selectedRelease = null;
                state.selectedM3uName = null;
                containerEl.querySelectorAll('.album-artist-btn').forEach(b => b.classList.remove('selected'));
                if (btn) btn.classList.add('selected');

                if (els.listSection) els.listSection.style.display = 'block';
                if (els.tracklistSection) els.tracklistSection.style.display = 'none';
                setResetVisible(false);
                if (els.listHeading) els.listHeading.textContent = `Albums by ${a.name}`;
                if (els.list) els.list.innerHTML = `<p class="bulk-intro-text">${escapeHtml(_musicbrainzQuip())}…</p>`;

                await loadAlbums(a);
            }

            async function selectAlbum(album, btn) {
                state.selectedRelease = album;
                containerEl.querySelectorAll('.album-list-btn').forEach(b => b.classList.remove('selected'));
                if (btn) btn.classList.add('selected');

                if (els.downloadBtn) { els.downloadBtn.disabled = false; els.downloadBtn.textContent = 'Download Album'; }
                setResetVisible(false);
                if (els.existingWarning) { els.existingWarning.style.display = 'none'; els.existingWarning.textContent = ''; }
                if (els.trackLegend) { els.trackLegend.style.display = 'none'; els.trackLegend.textContent = ''; }
                state.selectedM3uName = null;
                if (els.makeM3u) els.makeM3u.checked = false;
                if (els.m3uHint) { els.m3uHint.style.display = 'none'; els.m3uHint.textContent = ''; }
                if (els.tracklistSection) els.tracklistSection.style.display = 'block';
                if (els.tracklistHeading) els.tracklistHeading.textContent = `${album.title}${album.year ? ' (' + album.year + ')' : ''}`;
                if (els.tracklist) els.tracklist.innerHTML = '<li>Loading…</li>';
                if (els.downloadBtn) els.downloadBtn.disabled = true;

                try {
                    // The list only knows the release-group; the specific pressing
                    // to download is resolved here, lazily, for the one album the
                    // user actually opened (see fetch_artist_albums for why).
                    const resp = album.release_mbid
                        ? await apiFetch(`/api/albums/release/${encodeURIComponent(album.release_mbid)}/tracks`)
                        : await apiFetch(`/api/albums/release-group/${encodeURIComponent(album.release_group_mbid)}/tracks`);
                    if (!resp.ok) {
                        const detail = await _readApiErrorDetail(resp);
                        _renderRetryError(els.tracklist, `Failed to load tracklist: ${detail}`, () => selectAlbum(album, btn), true);
                        return;
                    }
                    const data = await resp.json();
                    if (!album.release_mbid && data.release_mbid) album.release_mbid = data.release_mbid;
                    const tracks = data.tracks || [];
                    if (!els.tracklist) return;
                    if (!tracks.length) {
                        els.tracklist.innerHTML = '<li>No tracks found.</li>';
                        return;
                    }
                    els.tracklist.innerHTML = '';
                    for (const t of tracks) {
                        const li = document.createElement('li');
                        li.className = 'album-track-item';
                        li.textContent = t.title;
                        els.tracklist.appendChild(li);
                    }
                    if (state.selectedArtist && els.existingWarning) {
                        try {
                            const params = new URLSearchParams({
                                artist: state.selectedArtist.name,
                                album_title: album.title,
                            });
                            const statusResp = await apiFetch(
                                `/api/albums/release/${encodeURIComponent(album.release_mbid)}/missing?${params.toString()}`
                            );
                            if (statusResp.ok) {
                                const statusData = await statusResp.json();
                                const existing = Number(statusData.existing_count || 0);
                                const missing = Number(statusData.missing_count || 0);
                                const total = Number(statusData.total_tracks || tracks.length || 0);

                                if (Array.isArray(statusData.tracks) && statusData.tracks.length === tracks.length) {
                                    const liEls = els.tracklist.querySelectorAll('li');
                                    statusData.tracks.forEach((st, idx) => {
                                        const li = liEls[idx];
                                        if (!li) return;
                                        li.classList.remove('album-track-existing', 'album-track-missing');
                                        li.classList.add(st.exists ? 'album-track-existing' : 'album-track-missing');
                                    });
                                }

                                if (els.trackLegend && existing > 0) {
                                    els.trackLegend.style.display = 'block';
                                    els.trackLegend.innerHTML = `<span class="missing">Green</span> = missing (will queue) &nbsp;·&nbsp; <span class="existing">dim</span> = already on disk`;
                                }

                                if (existing > 0) {
                                    els.existingWarning.style.display = 'block';
                                    if (missing <= 0) {
                                        els.existingWarning.textContent = `Album already exists in ${statusData.album_dir} (${existing}/${total} tracks). Download will queue nothing.`;
                                    } else {
                                        els.existingWarning.textContent = `${existing}/${total} track(s) already exist in ${statusData.album_dir}. Only ${missing} missing track(s) will be queued.`;
                                    }
                                }

                                const m3uFiles = Array.isArray(statusData.existing_m3u_files) ? statusData.existing_m3u_files : [];
                                if (statusData.has_existing_m3u && m3uFiles.length > 0) {
                                    state.selectedM3uName = m3uFiles[0];
                                    if (els.makeM3u) els.makeM3u.checked = true;
                                    if (els.m3uHint) {
                                        els.m3uHint.style.display = 'block';
                                        els.m3uHint.textContent = `Existing M3U found (${state.selectedM3uName}) - this will be updated.`;
                                    }
                                }
                            }
                        } catch (e) {
                            // Non-fatal; user can still download.
                        }
                    }
                    if (els.downloadBtn) { els.downloadBtn.disabled = false; els.downloadBtn.textContent = 'Download Album'; }
                } catch (e) {
                    _renderRetryError(els.tracklist, `Failed to load tracklist: ${e.message}`, () => selectAlbum(album, btn), true);
                }
            }

            function renderProgress(data) {
                const done = data.completed || 0;
                const total = data.total_tracks || 0;
                const failed = data.failed || 0;
                const pct = total > 0 ? Math.round((done / total) * 100) : 0;
                const statusText = data.complete
                    ? `Done: ${done}/${total} tracks${failed ? `, ${failed} failed` : ''}`
                    : `Downloading: ${done}/${total} tracks…`;
                return `<div class="bulk-progress-bar-container"><div class="bulk-progress-bar" style="width:${pct}%"></div></div>`
                    + `<p class="bulk-intro-text">${escapeHtml(statusText)}</p>`;
            }

            async function downloadAlbum() {
                if (!state.selectedArtist || !state.selectedRelease) return;
                const makeM3u = els.makeM3u?.checked || false;
                setResetVisible(false);
                if (els.downloadBtn) { els.downloadBtn.disabled = true; els.downloadBtn.textContent = 'Queuing…'; }

                try {
                    const resp = await apiFetch('/api/albums/download', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({
                            artist: state.selectedArtist.name,
                            album_title: state.selectedRelease.title,
                            release_mbid: state.selectedRelease.release_mbid,
                            make_m3u: makeM3u,
                            m3u_name: makeM3u ? (state.selectedM3uName || null) : null,
                            convert_audio: convertAudioCheckbox.checked,
                        }),
                    });
                    if (!resp.ok) {
                        const err = await resp.json().catch(() => ({}));
                        throw new Error(err.detail || 'Download failed');
                    }
                    const data = await resp.json();
                    if (els.progress) {
                        els.progress.style.display = 'block';
                        const warningLine = data.warning
                            ? `<p class="settings-hint-sm" style="margin:0 0 8px;">${escapeHtml(data.warning)}</p>`
                            : '';
                        els.progress.innerHTML =
                            `${warningLine}<p class="settings-hint-sm" style="margin:0 0 8px;">Saving to: <code>${escapeHtml(data.album_dir)}</code></p>`;
                    }

                    if (onQueued) { try { onQueued(data); } catch (e) {} }

                    if (!data.import_id) {
                        if (els.downloadBtn) { els.downloadBtn.disabled = true; els.downloadBtn.textContent = 'Already Complete'; }
                        setResetVisible(true);
                        return;
                    }

                    if (state.pollInterval) clearInterval(state.pollInterval);
                    const albumDirHint = `<p class="settings-hint-sm" style="margin:0 0 8px;">Saving to: <code>${escapeHtml(data.album_dir)}</code></p>`;
                    state.pollInterval = setInterval(async () => {
                        try {
                            const statusResp = await apiFetch(`/api/bulk-import/${data.import_id}/status`);
                            if (!statusResp.ok) return;
                            const statusData = await statusResp.json();
                            if (els.progress) els.progress.innerHTML = albumDirHint + renderProgress(statusData);
                            if (statusData.complete) {
                                clearInterval(state.pollInterval);
                                state.pollInterval = null;
                                if (els.downloadBtn) { els.downloadBtn.disabled = true; els.downloadBtn.textContent = 'Downloaded'; }
                                setResetVisible(true);
                            }
                        } catch (e) { /* transient, keep polling */ }
                    }, 2000);

                } catch (e) {
                    if (els.downloadBtn) { els.downloadBtn.disabled = false; els.downloadBtn.textContent = 'Download Album'; }
                    if (els.progress) {
                        els.progress.style.display = 'block';
                        els.progress.innerHTML = `<p class="error-text">Error: ${escapeHtml(e.message)}</p>`;
                    }
                }
            }

            function reset() {
                if (state.pollInterval) { clearInterval(state.pollInterval); state.pollInterval = null; }
                state.selectedRelease = null;
                state.selectedM3uName = null;

                if (els.artistInput) els.artistInput.value = '';
                if (els.artistResults) { els.artistResults.style.display = 'none'; els.artistResults.innerHTML = ''; }
                if (els.listSection) els.listSection.style.display = 'none';
                if (els.list) els.list.innerHTML = '';
                if (els.listHeading) els.listHeading.textContent = '';
                if (els.tracklistSection) els.tracklistSection.style.display = 'none';
                if (els.tracklist) els.tracklist.innerHTML = '';
                if (els.tracklistHeading) els.tracklistHeading.textContent = '';
                if (els.existingWarning) { els.existingWarning.style.display = 'none'; els.existingWarning.textContent = ''; }
                if (els.trackLegend) { els.trackLegend.style.display = 'none'; els.trackLegend.textContent = ''; }
                if (els.progress) { els.progress.style.display = 'none'; els.progress.innerHTML = ''; }
                if (els.downloadBtn) { els.downloadBtn.disabled = false; els.downloadBtn.textContent = 'Download Album'; }
                if (els.makeM3u) els.makeM3u.checked = false;
                if (els.m3uHint) { els.m3uHint.style.display = 'none'; els.m3uHint.textContent = ''; }
                setResetVisible(false);

                containerEl.scrollIntoView({ behavior: 'smooth', block: 'start' });
                if (els.artistInput) {
                    els.artistInput.focus({ preventScroll: true });
                    state.selectedArtist = null;
                } else if (artist) {
                    // Pinned artist, no search box to fall back on: hop straight
                    // back to their album list rather than showing a dead end.
                    state.selectedArtist = { mbid: artist.mbid, name: artist.name };
                    selectArtist(state.selectedArtist, null);
                } else {
                    state.selectedArtist = null;
                }
            }

            if (els.artistSearchBtn) els.artistSearchBtn.addEventListener('click', searchArtist);
            if (els.artistInput) els.artistInput.addEventListener('keydown', e => { if (e.key === 'Enter') searchArtist(); });
            if (els.downloadBtn) els.downloadBtn.addEventListener('click', downloadAlbum);
            if (els.resetBtn) els.resetBtn.addEventListener('click', reset);

            const handle = {
                el: containerEl,
                hasSelection() {
                    return !!(state.selectedRelease
                        || (state.selectedArtist && !artist)
                        || (els.artistInput && els.artistInput.value.trim())
                        || (els.artistResults && els.artistResults.innerHTML.trim()));
                },
                setArtist(mbid, name) {
                    return selectArtist({ mbid, name }, null);
                },
                searchFor(name) {
                    if (!els.artistInput) return;
                    els.artistInput.value = name;
                    return searchArtist();
                },
                async openAlbum(opts) {
                    // opts: { artistName, albumTitle, artistMbid?, releaseMbid? }
                    if (!opts.artistName || !opts.albumTitle) return;
                    if (els.artistInput) els.artistInput.value = opts.artistName;
                    state.selectedArtist = null;
                    state.selectedRelease = null;
                    state.selectedM3uName = null;
                    if (state.pollInterval) { clearInterval(state.pollInterval); state.pollInterval = null; }
                    if (els.downloadBtn) { els.downloadBtn.disabled = false; els.downloadBtn.textContent = 'Download Album'; }
                    if (els.tracklistSection) els.tracklistSection.style.display = 'none';
                    if (els.progress) els.progress.style.display = 'none';
                    setResetVisible(false);

                    if (opts.artistMbid && opts.releaseMbid) {
                        // Fast path: caller already resolved MBIDs (e.g. a MusicBrainz URL paste)
                        state.selectedArtist = { mbid: opts.artistMbid, name: opts.artistName };
                        if (els.artistResults) {
                            els.artistResults.style.display = '';
                            els.artistResults.innerHTML = '';
                            const btn = document.createElement('button');
                            btn.className = 'album-artist-btn selected';
                            btn.innerHTML = `<span class="album-artist-name">${escapeHtml(opts.artistName)}</span>`;
                            btn.addEventListener('click', () => selectArtist(state.selectedArtist, btn));
                            els.artistResults.appendChild(btn);
                        }
                        if (els.listSection) els.listSection.style.display = 'block';
                        if (els.listHeading) els.listHeading.textContent = `Albums by ${opts.artistName}`;
                        if (els.list) els.list.innerHTML = `<p class="bulk-intro-text">${escapeHtml(_musicbrainzQuip())}…</p>`;
                        const albums = await loadAlbums(state.selectedArtist) || [];
                        // The list only carries a release-group id, not the caller's
                        // specific release MBID, so preselect by title the same way
                        // the slow path below already does.
                        const matchedAlbum = findMatchingAlbum(albums, opts.albumTitle);
                        if (matchedAlbum) {
                            const matchedBtn = Array.from(els.list?.querySelectorAll('.album-list-btn') || [])
                                .find(b => b.querySelector('.album-list-title')?.textContent === matchedAlbum.title);
                            await selectAlbum(matchedAlbum, matchedBtn);
                        }
                    } else {
                        // Slow path: look up the artist, then fuzzy-match the album title
                        if (els.artistResults) {
                            els.artistResults.style.display = '';
                            els.artistResults.innerHTML = `<p class="bulk-intro-text">${escapeHtml(_musicbrainzQuip())}…</p>`;
                        }
                        try {
                            const artistResp = await apiFetch(`/api/albums/search-artist?q=${encodeURIComponent(opts.artistName)}`);
                            if (!artistResp.ok) throw new Error('Artist search failed');
                            const artistData = await artistResp.json();
                            const artists = artistData.artists || [];
                            if (!artists.length) {
                                if (els.artistResults) els.artistResults.innerHTML = '<p class="bulk-intro-text">No matching artist found on MusicBrainz.</p>';
                                return;
                            }
                            const bestArtist = artists[0];
                            state.selectedArtist = bestArtist;
                            if (els.artistResults) {
                                els.artistResults.innerHTML = '';
                                for (const a of artists) {
                                    const btn = document.createElement('button');
                                    btn.className = 'album-artist-btn';
                                    if (a.mbid === bestArtist.mbid) btn.classList.add('selected');
                                    btn.innerHTML = `<span class="album-artist-name">${escapeHtml(a.name)}</span>`
                                        + (a.disambiguation ? ` <span class="album-artist-disambig">${escapeHtml(a.disambiguation)}</span>` : '');
                                    btn.addEventListener('click', () => selectArtist(a, btn));
                                    els.artistResults.appendChild(btn);
                                }
                            }
                            if (els.listSection) els.listSection.style.display = 'block';
                            if (els.listHeading) els.listHeading.textContent = `Albums by ${bestArtist.name}`;
                            if (els.list) els.list.innerHTML = `<p class="bulk-intro-text">${escapeHtml(_musicbrainzQuip())}…</p>`;

                            const albums = await loadAlbums(bestArtist) || [];
                            if (!albums.length) return;

                            const matchedAlbum = findMatchingAlbum(albums, opts.albumTitle);
                            if (matchedAlbum) {
                                containerEl.querySelectorAll('.album-list-btn').forEach(b => b.classList.remove('selected'));
                                const matchedBtn = Array.from(els.list?.querySelectorAll('.album-list-btn') || [])
                                    .find(b => b.querySelector('.album-list-title')?.textContent === matchedAlbum.title);
                                await selectAlbum(matchedAlbum, matchedBtn);
                            } else {
                                showToast(`Album "${opts.albumTitle}" not found in MusicBrainz. Pick one manually.`, true);
                            }
                        } catch (e) {
                            if (els.artistResults) els.artistResults.innerHTML = `<p class="bulk-intro-text error-text">Failed: ${escapeHtml(e.message)}</p>`;
                        }
                    }
                },
                reset,
                destroy() {
                    if (state.pollInterval) clearInterval(state.pollInterval);
                    containerEl.innerHTML = '';
                },
            };

            if (state.selectedArtist) selectArtist(state.selectedArtist, null);

            return handle;
        }

        // -------------------------------------------------------------------
        // One-off album browsing (Artists tab, "browse without following") and
        // the Tracks-tab handoff into it.
        // -------------------------------------------------------------------

        // The one-off browser is the chooser's third option ("Download an Album").
        // It has no search box of its own: the artist has already been picked
        // upstairs, so a second search box would just be asking the same question
        // twice. The Tracks-tab handoff drives it through openAlbum() instead.

        let oneOffAlbumBrowserHandle = null;

        function ensureOneOffAlbumBrowser() {
            if (oneOffAlbumBrowserHandle) return oneOffAlbumBrowserHandle;
            const mountEl = document.getElementById('artistOneOffBrowserMount');
            if (!mountEl) return null;
            oneOffAlbumBrowserHandle = mountAlbumBrowser(mountEl, { showArtistSearch: false });
            return oneOffAlbumBrowserHandle;
        }

        function showOneOffAlbumSection(artistName) {
            hideArtistModePanels();
            const panel = document.getElementById('artistOneOffPanel');
            if (panel) panel.style.display = 'block';
            const nameEl = document.getElementById('artistOneOffName');
            if (nameEl) nameEl.textContent = artistName ? `Albums by ${artistName}` : 'Download an album';
            return ensureOneOffAlbumBrowser();
        }

        // -------------------------------------------------------------------
        // Bulk Import's copy of the same browser. The Albums tab is gone, so
        // its artist/release browsing lives here as well as on Artists; one
        // component, mounted twice, rather than two things to keep in step.
        // -------------------------------------------------------------------

        let bulkAlbumBrowserHandle = null;

        function ensureBulkAlbumBrowser() {
            if (bulkAlbumBrowserHandle) return bulkAlbumBrowserHandle;
            const mountEl = document.getElementById('bulkAlbumBrowserMount');
            if (!mountEl) return null;
            bulkAlbumBrowserHandle = mountAlbumBrowser(mountEl, { showArtistSearch: true });
            return bulkAlbumBrowserHandle;
        }

        document.getElementById('bulkAlbumBrowseToggleBtn')?.addEventListener('click', () => {
            const section = document.getElementById('bulkAlbumBrowseSection');
            const toggleBtn = document.getElementById('bulkAlbumBrowseToggleBtn');
            if (!section || !toggleBtn) return;
            const visible = section.style.display !== 'none';
            if (visible) {
                section.style.display = 'none';
                toggleBtn.textContent = "+ Browse an artist's albums";
            } else {
                section.style.display = 'block';
                toggleBtn.textContent = "− Browse an artist's albums";
                ensureBulkAlbumBrowser();
            }
        });

        function albumHandoffArtist() {
            const query = lastCompletedSearchQuery.trim();
            if (!query || !lastResults.length) return '';

            const parsed = parseArtistTitle(query);
            if (parsed?.artist) return parsed.artist;

            const resultWithArtist = lastResults.find(result => String(result.artist || '').trim());
            if (resultWithArtist) return String(resultWithArtist.artist).trim();

            // An artist-only search is already suitable input for MusicBrainz.
            return query.includes(' - ') ? '' : query;
        }

        function primeAlbumSearchFromResults() {
            if (currentTab !== 'artists') return;

            const artistName = albumHandoffArtist();
            if (!artistName) return;

            // Album work is deliberately sticky. Returning to a search or selected
            // release must never be replaced just because Tracks still has context.
            // The album browser lost its own search box to the chooser, so the
            // handoff now pre-fills the chooser's artist box instead. Only when it
            // is empty and nothing is mid-flight; inheriting context is a courtesy,
            // not a licence to type over what somebody is already doing.
            const input = document.getElementById('artistSearchInput');
            if (!input || input.value.trim()) return;
            if (document.getElementById('artistModeChooser')?.style.display !== 'none') return;
            if (oneOffAlbumBrowserHandle && oneOffAlbumBrowserHandle.hasSelection()) return;
            input.value = artistName;
        }

        async function openAlbumFromSearch(opts) {
            // opts: { artistName, albumTitle, artistMbid?, releaseMbid? }
            if (!opts.artistName || !opts.albumTitle) return;

            // Switch to the Artists tab and pop the one-off browser open; this
            // handoff never assumes you want to follow the artist, just download
            // one album.
            const artistsTabBtn = document.querySelector('[data-tab="artists"]');
            if (artistsTabBtn) artistsTabBtn.click();
            const handle = showOneOffAlbumSection(opts.artistName);
            if (!handle) return;
            await handle.openAlbum(opts);
            document.getElementById('artistOneOffPanel')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
        }

        let downloadablePage = 1;

        async function loadDownloadable(delta = 0) {
            downloadablePage = Math.max(1, downloadablePage + delta);
            const listEl = document.getElementById('downloadableList');
            const pagerEl = document.getElementById('downloadablePager');
            const infoEl = document.getElementById('downloadablePagerInfo');
            const prevBtn = document.getElementById('downloadablePrevBtn');
            const nextBtn = document.getElementById('downloadableNextBtn');
            if (!listEl) return;
            listEl.innerHTML = '<div class="loading"><div class="spinner"></div></div>';
            try {
                const res = await apiFetch(`/api/jobs/downloadable?page=${downloadablePage}&per_page=15`);
                const data = await res.json();
                if (!data.jobs || data.jobs.length === 0) {
                    listEl.innerHTML = '<div class="empty-state"><p>No completed downloads yet.</p></div>';
                    pagerEl.style.display = 'none';
                    return;
                }
                listEl.innerHTML = data.jobs.map(job => {
                    const label = job.artist ? `${escapeHtml(job.artist)} \u2013 ${escapeHtml(job.title)}` : escapeHtml(job.title);
                    const date = job.completed_at ? formatTimeAgo(job.completed_at) : '';
                    return `<div style="display:flex;align-items:center;gap:8px;padding:6px 0;border-bottom:1px solid var(--border);font-size:13px;">
                        <span style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${escapeHtml(job.artist||'')} - ${escapeHtml(job.title)}">${label}</span>
                        ${date ? `<span style="font-size:11px;color:var(--text-secondary);white-space:nowrap;flex-shrink:0;">${date}</span>` : ''}
                        <button onclick="saveJobToDevice('${escapeAttr(job.id || '')}')" style="flex-shrink:0;padding:4px 10px;font-size:11px;font-family:inherit;font-weight:600;background:var(--bg-tertiary);color:var(--text-secondary);border:1px solid var(--border);border-radius:6px;cursor:pointer;white-space:nowrap;">
                            <i class="fa-solid fa-download"></i> Save
                        </button>
                    </div>`;
                }).join('');
                if (data.pages > 1) {
                    pagerEl.style.display = 'flex';
                    infoEl.textContent = `Page ${data.page} of ${data.pages} (${data.total} tracks)`;
                    prevBtn.disabled = data.page <= 1;
                    nextBtn.disabled = data.page >= data.pages;
                } else {
                    pagerEl.style.display = 'none';
                }
            } catch (e) {
                listEl.innerHTML = '<div class="empty-state"><p>Failed to load downloads.</p></div>';
            }
        }

        async function clearQueue() {
            if (!confirm('Clear all completed, failed, and stale downloads from the queue?')) {
                return;
            }

            clearQueueBtn.disabled = true;

            try {
                const response = await apiFetch('/api/jobs/cleanup?confirm=true', {
                    method: 'DELETE'
                });

                if (!response.ok) throw new Error('Failed to clear queue');

                const data = await response.json();
                showToast(`Cleared ${data.deleted} job(s)`);
                loadJobs();
            } catch (error) {
                showToast('Failed to clear queue', true);
            } finally {
                clearQueueBtn.disabled = false;
            }
        }

        async function resetStats() {
            if (!confirm('Reset all dashboard stats? This clears completed/failed job history and search history.')) {
                return;
            }

            resetStatsBtn.disabled = true;

            try {
                const response = await apiFetch('/api/stats?confirm=true', {
                    method: 'DELETE'
                });

                if (!response.ok) throw new Error('Failed to reset stats');

                const data = await response.json();
                showToast(`Stats reset (${data.deleted_jobs} jobs, ${data.deleted_searches} searches)`);
                loadJobs();
                loadStats();
            } catch (error) {
                showToast('Failed to reset stats', true);
            } finally {
                resetStatsBtn.disabled = false;
            }
        }

        function escapeHtml(text) {
            const div = document.createElement('div');
            div.textContent = text;
            return div.innerHTML;
        }

        function escapeAttr(text) {
            // Safe for use inside single-quoted JS string literals in HTML attributes
            return escapeHtml(String(text)).replace(/'/g, '&#39;').replace(/\\/g, '\\\\');
        }

        // WARNING: jsStr is NOT safe inside double-quoted onclick attributes!
        // &quot; decoded by the HTML parser terminates the attribute boundary.
        // For onclick handlers, use escapeAttr with single-quoted JS strings:
        //   onclick="fn('${escapeAttr(val)}')"
        function jsStr(text) {
            return escapeHtml(JSON.stringify(String(text)));
        }

        // Line counter (no limit)
        function updateLineCounter() {
            const lines = bulkInput.value.split('\n').filter(line => line.trim());
            const count = lines.length;
            lineCounter.textContent = `${count} line${count !== 1 ? 's' : ''}`;
            lineCounter.style.color = 'var(--text-secondary)';
            lineCounter.style.background = 'var(--bg-tertiary)';
        }

        bulkInput.addEventListener('input', updateLineCounter);

        // File upload handler
        fileUpload.addEventListener('change', (e) => {
            const file = e.target.files[0];
            if (!file) return;

            fileName.textContent = file.name;

            const reader = new FileReader();
            reader.onload = (event) => {
                bulkInput.value = event.target.result;
                updateLineCounter();
            };
            reader.readAsText(file);
        });

        // Event listeners
        searchBtn.addEventListener('click', search);
        searchInput.addEventListener('keypress', (e) => {
            if (e.key === 'Enter') {
                search();
            }
        });
        searchInput.addEventListener('focus', () => {
            if (searchInput.value.trim() === '') {
                showSearchHistory();
            }
        });
        searchInput.addEventListener('blur', hideSearchHistory);
        searchInput.addEventListener('input', () => {
            searchClearBtn.style.display = searchInput.value ? '' : 'none';
            if (searchInput.value.trim() === '') {
                showSearchHistory();
            } else {
                hideSearchHistory();
            }
        });
        searchClearBtn.addEventListener('click', () => {
            setSearchValue('');
            searchInput.focus();
            // Reset results back to the empty state
            stopPreview();
            lastCompletedSearchQuery = '';
            lastResults = [];
            relatedSuggestions.style.display = 'none';
            exploreBar.style.display = 'none';
            const _destRow3 = document.getElementById('destinationPickerRow');
            if (_destRow3) _destRow3.style.display = 'none';
            resultsTab.innerHTML = `
                <div class="empty-state">
                    <div class="empty-state-icon"><i class="fa-solid fa-headphones"></i></div>
                    <p>Search for music to get started</p>
                    <p style="font-size: 12px; margin-top: 8px; opacity: 0.6;">Hover over results to preview</p>
                </div>
            `;
            showSearchHistory();
        });
        watchedList.addEventListener('click', (e) => {
            const btn = e.target.closest('button[data-action]');
            if (!btn || !watchedList.contains(btn)) return;

            const action = btn.dataset.action;
            if (action === 'retry-missing-track') {
                retryMissingTrack(
                    btn.dataset.playlistId || '',
                    btn.dataset.artist || '',
                    btn.dataset.title || '',
                    btn.dataset.rowId || ''
                );
                return;
            }

            if (action === 'search-missing-track') {
                searchMissingTrack(
                    btn.dataset.playlistId || '',
                    btn.dataset.playlistName || '',
                    btn.dataset.artist || '',
                    btn.dataset.title || ''
                );
                return;
            }

            if (action === 'find-missing-track-versions') {
                openMissingTrackVersionsModal(
                    btn.dataset.playlistId || '',
                    btn.dataset.playlistName || '',
                    btn.dataset.artist || '',
                    btn.dataset.title || '',
                    btn.dataset.rowId || ''
                );
                return;
            }

            if (action === 'replace-watched-track') {
                replaceTrack(
                    btn.dataset.playlistId || '',
                    btn.dataset.playlistName || '',
                    btn.dataset.artist || '',
                    btn.dataset.title || '',
                    btn.dataset.trackHash || '',
                    btn.dataset.rowId || ''
                );
                return;
            }

            if (action === 'refresh-watched-playlist') {
                refreshWatchedPlaylist(btn.dataset.playlistId || '');
                return;
            }

            if (action === 'toggle-missing-tracks') {
                toggleMissingTracks(btn.dataset.playlistId || '');
                return;
            }

            if (action === 'toggle-watched-playlist') {
                toggleWatchedPlaylist(
                    btn.dataset.playlistId || '',
                    btn.dataset.enabled === 'true' ? 'false' : 'true'
                );
                return;
            }

            if (action === 'toggle-playlist-track-list') {
                toggleTrackList(
                    btn.dataset.playlistId || '',
                    btn.dataset.playlistName || ''
                );
                return;
            }

            if (action === 'copy-watched-playlist-url') {
                copyWatchedPlaylistUrl(btn.dataset.url || '');
                return;
            }

            if (action === 'delete-watched-playlist') {
                deleteWatchedPlaylist(
                    btn.dataset.playlistId || '',
                    btn.dataset.playlistName || ''
                );
            }
        });
        watchedArtistList.addEventListener('click', (e) => {
            const btn = e.target.closest('button[data-action]');
            if (!btn || !watchedArtistList.contains(btn)) return;

            const action = btn.dataset.action;
            if (action === 'save-job-to-device') {
                saveJobToDevice(btn.dataset.jobId || '');
                return;
            }

            if (action === 'retry-all-artist-missing') {
                retryAllArtistMissing(
                    btn.dataset.artistId || '',
                    btn
                );
                return;
            }

            if (action === 'retry-artist-track') {
                retryArtistTrack(
                    btn.dataset.artistId || '',
                    btn.dataset.artist || '',
                    btn.dataset.title || '',
                    btn
                );
                return;
            }

            if (action === 'refresh-watched-artist') {
                refreshWatchedArtist(btn.dataset.artistId || '');
                return;
            }

            if (action === 'toggle-artist-missing-tracks') {
                toggleArtistMissingTracks(btn.dataset.artistId || '');
                return;
            }

            if (action === 'toggle-watched-artist') {
                toggleWatchedArtist(
                    btn.dataset.artistId || '',
                    btn.dataset.enabled === 'true' ? 'false' : 'true'
                );
                return;
            }

            if (action === 'toggle-artist-track-list') {
                toggleArtistTrackList(
                    btn.dataset.artistId || '',
                    btn.dataset.artistName || ''
                );
                return;
            }

            if (action === 'toggle-artist-albums') {
                openArtistAlbumPanel(btn.dataset.artistId || '');
                return;
            }

            if (action === 'delete-watched-artist') {
                deleteWatchedArtist(
                    btn.dataset.artistId || '',
                    btn.dataset.artistName || ''
                );
            }
        });
        bulkImportBtn.addEventListener('click', bulkImport);
        clearQueueBtn.addEventListener('click', clearQueue);
        document.getElementById('emptyTrashBtn').addEventListener('click', emptyTrash);
        resetStatsBtn.addEventListener('click', resetStats);

        if (queueHistorySort) {
            const savedQueueSort = localStorage.getItem(userStorageKey('queueHistorySort'));
            if (savedQueueSort && Array.from(queueHistorySort.options).some(option => option.value === savedQueueSort)) {
                queueHistorySort.value = savedQueueSort;
                queueHistorySortMode = savedQueueSort;
            }
            queueHistorySort.addEventListener('change', () => {
                queueHistorySortMode = queueHistorySort.value;
                localStorage.setItem(userStorageKey('queueHistorySort'), queueHistorySortMode);
                queuePage = 1;
                renderQueuePage();
            });
        }

        // Queue search. Deliberately not remembered between visits: a sticky filter
        // you forgot you set is how an empty Queue tab starts looking like a crisis.
        const queueSearchInput = document.getElementById('queueSearchInput');
        const queueSearchClearBtn = document.getElementById('queueSearchClearBtn');
        if (queueSearchInput) {
            const applyQueueSearch = (term) => {
                const next = (term || '').trim();
                if (next === queueSearchTerm) return;
                queueSearchTerm = next;
                queuePage = 1;
                if (queueSearchClearBtn) {
                    queueSearchClearBtn.style.display = queueSearchTerm ? 'flex' : 'none';
                }
                loadJobs(false);
            };

            queueSearchInput.addEventListener('input', () => {
                if (queueSearchClearBtn) {
                    queueSearchClearBtn.style.display = queueSearchInput.value ? 'flex' : 'none';
                }
                clearTimeout(queueSearchDebounce);
                // Long enough not to hammer SQLite on every keystroke, short enough
                // that it still feels like typing rather than posting a letter.
                queueSearchDebounce = setTimeout(() => applyQueueSearch(queueSearchInput.value), 250);
            });

            queueSearchInput.addEventListener('keydown', (event) => {
                if (event.key === 'Escape') {
                    clearTimeout(queueSearchDebounce);
                    queueSearchInput.value = '';
                    applyQueueSearch('');
                } else if (event.key === 'Enter') {
                    clearTimeout(queueSearchDebounce);
                    applyQueueSearch(queueSearchInput.value);
                }
            });

            if (queueSearchClearBtn) {
                queueSearchClearBtn.addEventListener('click', () => {
                    clearTimeout(queueSearchDebounce);
                    queueSearchInput.value = '';
                    applyQueueSearch('');
                    queueSearchInput.focus();
                });
            }
        }

        // Minimum-quality filter: remembered between searches, since somebody who
        // only wants lossless generally still only wants lossless five minutes later.
        const qualityFilterSelect = document.getElementById('qualityFilterSelect');
        const qualityFilterKeepUnknown = document.getElementById('qualityFilterKeepUnknown');
        if (qualityFilterSelect) {
            const savedTier = localStorage.getItem('mg_min_quality_tier');
            if (savedTier !== null) qualityFilterSelect.value = savedTier;
            qualityFilterSelect.addEventListener('change', () => {
                localStorage.setItem('mg_min_quality_tier', qualityFilterSelect.value);
                applyQualityFilter();
            });
        }
        if (qualityFilterKeepUnknown) {
            const savedKeep = localStorage.getItem('mg_quality_keep_unknown');
            if (savedKeep !== null) qualityFilterKeepUnknown.checked = savedKeep === '1';
            qualityFilterKeepUnknown.addEventListener('change', () => {
                localStorage.setItem('mg_quality_keep_unknown', qualityFilterKeepUnknown.checked ? '1' : '0');
                applyQualityFilter();
            });
        }

        // Preview button on desktop: off by default, so hover-to-preview stays the
        // behaviour everybody already knows.
        const desktopPreviewButtonToggle = document.getElementById('settingDesktopPreviewButton');
        if (desktopPreviewButtonToggle) {
            desktopPreviewButtonToggle.addEventListener('change', () => {
                localStorage.setItem(
                    userStorageKey('desktopPreviewButton'),
                    desktopPreviewButtonToggle.checked ? '1' : '0'
                );
                stopPreview();  // whatever was playing belonged to the old rules
                applyDesktopPreviewButton();
            });
        }
        applyDesktopPreviewButton();

        // =============================================================
        // Album name / URL routing (Bulk Import as the general acquisition
        // input). Three album-shaped things share one confirmation panel:
        //   - Spotify/Apple album URLs, via resolve-album-url (confidence gate)
        //   - MusicBrainz search-release results (artist optional; soundtracks,
        //     compilations etc. are first-class, see "Trainspotting" test case)
        //   - The candidate a user picks from either of the above needs its
        //     release_mbid resolved before /api/albums/download will take it.
        // All three end up at queueAlbumAndShowProgress(), which is the only
        // place that actually calls the download endpoint, so there is exactly
        // one code path doing the queueing bit.
        // =============================================================

        function hideAlbumMatchPanel() {
            if (!albumMatchPanel) return;
            albumMatchPanel.style.display = 'none';
            albumMatchPanel.innerHTML = '';
        }

        // A search-release / resolve-album-url candidate: {title, artist, year,
        // release_group_mbid, release_mbid, primary_type, secondary_types, score}.
        // release_mbid is null by design for search-release results (resolving
        // one costs a rate-limited MusicBrainz request per candidate); it's
        // usually present for resolve-album-url's single best match.
        function _albumCandidateTypeLabel(c) {
            const parts = [c.primary_type, ...(Array.isArray(c.secondary_types) ? c.secondary_types : [])].filter(Boolean);
            return parts.join(' · ');
        }

        function renderAlbumCandidates(candidates, headerText) {
            if (!albumMatchPanel) return;
            albumMatchPanel.style.display = 'block';
            albumMatchPanel.innerHTML = '';

            const heading = document.createElement('p');
            heading.className = 'bulk-intro-text';
            heading.textContent = headerText;
            albumMatchPanel.appendChild(heading);

            const list = document.createElement('div');
            list.className = 'album-list';
            for (const c of candidates) {
                const title = c.title || 'Unknown album';
                const typeLabel = _albumCandidateTypeLabel(c);
                const btn = document.createElement('button');
                btn.type = 'button';
                btn.className = 'album-list-btn';
                btn.innerHTML = `<span class="album-list-title">${escapeHtml(title)}</span>`
                    + (c.artist ? ` <span class="album-artist-disambig">${escapeHtml(c.artist)}</span>` : '')
                    + (c.year ? ` <span class="album-list-year">${escapeHtml(String(c.year))}</span>` : '')
                    + (typeLabel ? ` <span class="album-list-year">(${escapeHtml(typeLabel)})</span>` : '');
                btn.addEventListener('click', () => resolveAndQueueCandidate(c, btn));
                list.appendChild(btn);
            }
            albumMatchPanel.appendChild(list);

            const cancelBtn = document.createElement('button');
            cancelBtn.type = 'button';
            cancelBtn.className = 'btn btn-ghost';
            cancelBtn.textContent = 'None of these';
            cancelBtn.addEventListener('click', hideAlbumMatchPanel);
            albumMatchPanel.appendChild(cancelBtn);
        }

        // fields: {artist, album (or album_title), release_mbid}. Shows what's
        // about to be queued and requires an explicit click either way; the
        // confidence gate isn't a licence to skip showing the match, it just
        // decides whether the user also has to pick from alternatives first.
        function renderAlbumConfirmCard(fields, headerText) {
            if (!albumMatchPanel) return;
            albumMatchPanel.style.display = 'block';
            albumMatchPanel.innerHTML = '';

            const wrap = document.createElement('div');
            wrap.className = 'album-existing-warning';

            const p = document.createElement('p');
            p.style.margin = '0 0 8px';
            p.textContent = headerText;
            wrap.appendChild(p);

            if (!fields.release_mbid) {
                const warn = document.createElement('p');
                warn.style.margin = '0';
                warn.textContent = "MusicBrainz didn't settle on a specific release for this one just now; try again in a moment.";
                wrap.appendChild(warn);
                albumMatchPanel.appendChild(wrap);
                return;
            }

            const btnRow = document.createElement('div');
            btnRow.style.display = 'flex';
            btnRow.style.gap = '8px';
            btnRow.style.marginTop = '4px';

            const queueBtn = document.createElement('button');
            queueBtn.type = 'button';
            queueBtn.className = 'btn btn-primary';
            queueBtn.textContent = 'Queue Album';
            queueBtn.addEventListener('click', () => queueAlbumAndShowProgress(fields, queueBtn));

            const cancelBtn = document.createElement('button');
            cancelBtn.type = 'button';
            cancelBtn.className = 'btn btn-ghost';
            cancelBtn.textContent = 'Not this one';
            cancelBtn.addEventListener('click', hideAlbumMatchPanel);

            btnRow.appendChild(queueBtn);
            btnRow.appendChild(cancelBtn);
            wrap.appendChild(btnRow);
            albumMatchPanel.appendChild(wrap);
        }

        // A candidate from search-release (release_mbid always null) or from
        // resolve-album-url's `candidates` list (usually null too, same reason).
        // Either way, resolve-release-group is the bridge to a real release_mbid
        // before /api/albums/download will take it.
        async function resolveAndQueueCandidate(candidate, btnEl) {
            if (candidate.release_mbid) {
                renderAlbumConfirmCard(
                    { artist: candidate.artist, album: candidate.title, release_mbid: candidate.release_mbid },
                    `Queue: ${candidate.artist ? candidate.artist + ' – ' : ''}${candidate.title}?`
                );
                return;
            }
            if (!candidate.release_group_mbid) {
                showToast('Not enough information to queue this album.', true);
                return;
            }
            if (btnEl) { btnEl.disabled = true; btnEl.textContent = 'Resolving…'; }
            try {
                const resp = await apiFetch('/api/albums/resolve-release-group', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ release_group_mbid: candidate.release_group_mbid }),
                });
                const data = await resp.json().catch(() => ({}));
                if (!resp.ok) throw new Error(data.detail || 'Could not resolve that release');
                renderAlbumConfirmCard(
                    { artist: data.artist || candidate.artist, album: data.album_title || candidate.title, release_mbid: data.release_mbid },
                    `Queue: ${data.artist || candidate.artist} – ${data.album_title || candidate.title}?`
                );
            } catch (e) {
                showToast(e.message, true);
                if (btnEl) { btnEl.disabled = false; btnEl.textContent = 'Retry'; }
            }
        }

        // The one place that actually calls POST /api/albums/download for
        // everything routed through this panel (album URLs, search-release
        // picks). fields: {artist, album, release_mbid}.
        async function queueAlbumAndShowProgress(fields, btnEl) {
            if (btnEl) { btnEl.disabled = true; btnEl.textContent = 'Queuing…'; }
            const makeM3u = createPlaylistCheckbox.checked;
            try {
                const resp = await apiFetch('/api/albums/download', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        artist: fields.artist,
                        album_title: fields.album,
                        release_mbid: fields.release_mbid,
                        make_m3u: makeM3u,
                        m3u_name: makeM3u ? (playlistNameInput.value.trim() || null) : null,
                        convert_audio: convertAudioCheckbox.checked,
                    }),
                });
                const data = await resp.json().catch(() => ({}));
                if (!resp.ok) throw new Error(data.detail || 'Failed to queue album');

                const label = `${fields.artist} - ${fields.album}`;
                if (!data.import_id) {
                    showToast(data.already_queued ? `"${label}" is already queued` : `"${label}" is already complete, nothing to fetch`);
                } else {
                    const queued = data.queued_count ?? 0;
                    showToast(`Queued ${queued} track${queued === 1 ? '' : 's'} from "${label}"`);
                }
                hideAlbumMatchPanel();
                spotifyUrlInput.value = '';
            } catch (e) {
                showToast(e.message, true);
                if (btnEl) { btnEl.disabled = false; btnEl.textContent = 'Queue Album'; }
            }
        }

        // ALBUM url -> resolve-album-url -> confidence gate. Handles Spotify,
        // Apple Music, YouTube Music, Beatport, Tidal and Monochrome.
        // recognised:false falls through to the ordinary playlist path silently
        // (it means "not actually an album url", not an error).
        async function fetchAlbumUrl(url) {
            spotifyError.style.display = 'none';
            spotifyError.style.color = '';
            hideAlbumMatchPanel();
            fetchSpotifyBtn.disabled = true;
            fetchSpotifyBtn.textContent = 'Looking up album…';
            try {
                const resp = await apiFetch('/api/albums/resolve-album-url', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ url }),
                });
                if (resp.status === 404) {
                    spotifyError.textContent = 'Album URL matching is not available on this server yet.';
                    spotifyError.style.display = 'block';
                    return;
                }
                const data = await resp.json().catch(() => ({}));
                if (!resp.ok) throw new Error(data.detail || 'Could not resolve that album URL');

                if (!data.recognised) {
                    // Not actually an album url after all (or the service call
                    // fell over); ordinary playlist path, same url.
                    await doPlaylistFetch(url, false);
                    return;
                }

                if (data.confident && data.match) {
                    renderAlbumConfirmCard(
                        { artist: data.match.artist, album: data.match.title, release_mbid: data.match.release_mbid },
                        `Matched: ${data.match.artist} – ${data.match.title}`
                    );
                } else if (Array.isArray(data.candidates) && data.candidates.length) {
                    renderAlbumCandidates(data.candidates, `Not confident which album that is. Pick one:`);
                } else if (data.service === 'youtube') {
                    // OLAK5uy_ album playlists were importable as ordinary
                    // playlists before album support existed; an unmatched
                    // one falls back to that rather than dead-ending, since
                    // (unlike Spotify/Apple album urls) it's a genuine
                    // playlist url too.
                    await doPlaylistFetch(url, false);
                } else {
                    spotifyError.textContent = `Couldn't confidently match that to a MusicBrainz album (scraped as "${data.artist || '?'} - ${data.album || '?'}"). Try typing the album name in this box instead.`;
                    spotifyError.style.display = 'block';
                }
            } catch (e) {
                spotifyError.textContent = e.message;
                spotifyError.style.display = 'block';
            } finally {
                fetchSpotifyBtn.disabled = false;
                fetchSpotifyBtn.textContent = 'Fetch';
            }
        }

        // Plain text, no URL: search MusicBrainz release-groups by name. "Artist
        // - Album" splits on the same dash convention as the bulk track list;
        // bare text with no separator searches by album title alone, on purpose
        // (an artist is not required, see search_release_groups' docstring).
        async function searchReleaseByName(query) {
            let artist = '';
            let album = query;
            const dashMatch = query.match(/^(.+?)\s+[-–—]\s+(.+)$/);
            if (dashMatch) {
                artist = dashMatch[1].trim();
                album = dashMatch[2].trim();
            }

            spotifyError.style.display = 'none';
            spotifyError.style.color = '';
            hideAlbumMatchPanel();
            fetchSpotifyBtn.disabled = true;
            fetchSpotifyBtn.textContent = 'Searching…';
            try {
                const params = new URLSearchParams({ q: album });
                if (artist) params.set('artist', artist);
                const resp = await apiFetch(`/api/albums/search-release?${params.toString()}`);
                if (resp.status === 404) {
                    spotifyError.textContent = 'Album search is not available on this server yet.';
                    spotifyError.style.display = 'block';
                    return;
                }
                const data = await resp.json().catch(() => ({}));
                if (!resp.ok) throw new Error(data.detail || 'Album search failed');

                const releases = data.releases || [];
                if (!releases.length) {
                    spotifyError.textContent = `No albums found matching "${query}". Paste it into the song list below to search it as a track instead.`;
                    spotifyError.style.display = 'block';
                    return;
                }
                renderAlbumCandidates(releases, `Found ${releases.length} album${releases.length === 1 ? '' : 's'} matching "${query}":`);
            } catch (e) {
                spotifyError.textContent = e.message;
                spotifyError.style.display = 'block';
            } finally {
                fetchSpotifyBtn.disabled = false;
                fetchSpotifyBtn.textContent = 'Fetch';
            }
        }

        // MusicBrainz release URL -> straight into the album download pipeline.
        // No scraping, no fuzzy matching: the MBID is in the URL, so we resolve it
        // to artist/title and queue the album properly rather than flattening it
        // into a list of "Artist - Title" lines that lands in Singles.
        async function fetchMusicBrainzRelease(url) {
            spotifyError.style.display = 'none';
            fetchSpotifyBtn.disabled = true;
            fetchSpotifyBtn.textContent = 'Looking up release…';
            try {
                const resolveResp = await apiFetch('/api/albums/resolve-url', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ url }),
                });
                const album = await resolveResp.json().catch(() => ({}));
                if (!resolveResp.ok) {
                    throw new Error(album.detail || 'Could not resolve that MusicBrainz URL');
                }

                fetchSpotifyBtn.textContent = 'Queuing album…';
                const makeM3u = createPlaylistCheckbox.checked;
                const dlResp = await apiFetch('/api/albums/download', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        artist: album.artist,
                        album_title: album.album_title,
                        release_mbid: album.release_mbid,
                        make_m3u: makeM3u,
                        m3u_name: makeM3u ? (playlistNameInput.value.trim() || null) : null,
                        convert_audio: convertAudioCheckbox.checked,
                    }),
                });
                const data = await dlResp.json().catch(() => ({}));
                if (!dlResp.ok) {
                    throw new Error(data.detail || 'Failed to queue album');
                }

                const label = `${album.artist} - ${album.album_title}`;
                if (!data.import_id) {
                    showToast(`"${label}" is already complete, nothing to fetch`);
                } else {
                    const queued = data.queued_count ?? album.track_count ?? 0;
                    showToast(`Queued ${queued} track${queued === 1 ? '' : 's'} from "${label}"`);
                }
                spotifyUrlInput.value = '';
            } catch (error) {
                spotifyError.textContent = error.message;
                spotifyError.style.color = '';
                spotifyError.style.display = 'block';
            } finally {
                fetchSpotifyBtn.disabled = false;
                fetchSpotifyBtn.textContent = 'Fetch';
            }
        }

        // =============================================================
        // Long-form YouTube splitting (Bulk Import Fetch box only). A single
        // video over the length threshold gets cut into tagged tracks via
        // ffmpeg instead of downloaded whole. Detection never guesses blind:
        // it prefers YouTube's own chapters, falls back to a cue sheet hunted
        // out of the comments, and always lands on an editable table the user
        // confirms before anything downloads.
        // =============================================================

        function hideLongformPanel() {
            if (!longformSplitPanel) return;
            longformSplitPanel.style.display = 'none';
            longformSplitPanel.innerHTML = '';
            longformDetectedContext = null;
            if (longformSplitPollInterval) {
                clearInterval(longformSplitPollInterval);
                longformSplitPollInterval = null;
            }
        }

        function _longformDetectedViaLabel(detectedVia) {
            if (detectedVia === 'chapters') return 'Detected from YouTube chapters';
            if (detectedVia === 'description') return 'Detected from a cue sheet in the video description';
            if (detectedVia === 'comments') return 'Detected from a comment cue sheet';
            return 'No cue points found automatically — enter them manually below';
        }

        function _formatTimestamp(totalSeconds) {
            const s = Math.max(0, Math.round(totalSeconds || 0));
            const h = Math.floor(s / 3600);
            const m = Math.floor((s % 3600) / 60);
            const sec = s % 60;
            const pad = n => String(n).padStart(2, '0');
            return h > 0 ? `${h}:${pad(m)}:${pad(sec)}` : `${m}:${pad(sec)}`;
        }

        function _parseTimestampInput(text) {
            const parts = String(text || '').trim().split(':').map(p => parseInt(p, 10));
            if (!parts.length || parts.some(isNaN)) return 0;
            let seconds = 0;
            for (const p of parts) seconds = seconds * 60 + p;
            return seconds;
        }

        // Mirrors longform_split.py's parse_cue_sheet_text/_fill_end_times, for
        // the "paste your own cue sheet" fallback so it doesn't need a round
        // trip to re-parse text the user already typed.
        function _parseCueSheetTextJs(text, duration) {
            const lineRe = /^\s*(?:\d+[.)]\s*)?[\[(]?(\d{1,2}(?::\d{2}){1,2})[\])]?\s*[-–—:]?\s*(.+?)\s*$/;
            const points = [];
            for (const line of String(text || '').split('\n')) {
                const m = lineRe.exec(line);
                if (!m) continue;
                const title = m[2].replace(/^[-–—:\s]+|[-–—:\s]+$/g, '');
                if (!title) continue;
                points.push({ start_seconds: _parseTimestampInput(m[1]), title });
            }
            const segments = [];
            for (let i = 0; i < points.length; i++) {
                const end = i + 1 < points.length ? points[i + 1].start_seconds : (duration || points[i].start_seconds);
                segments.push({ start_seconds: points[i].start_seconds, end_seconds: end, title: points[i].title, artist: '' });
            }
            return segments;
        }

        function _buildLongformRow(segment) {
            const row = document.createElement('div');
            row.className = 'longform-row';

            const startInput = document.createElement('input');
            startInput.type = 'text';
            startInput.className = 'longform-row-input longform-start';
            startInput.placeholder = '0:00';
            startInput.value = _formatTimestamp(segment.start_seconds || 0);

            const endInput = document.createElement('input');
            endInput.type = 'text';
            endInput.className = 'longform-row-input longform-end';
            endInput.placeholder = '0:00';
            endInput.value = _formatTimestamp(segment.end_seconds || 0);

            const titleInput = document.createElement('input');
            titleInput.type = 'text';
            titleInput.className = 'longform-row-input longform-title';
            titleInput.placeholder = 'Track title';
            titleInput.value = segment.title || '';

            const artistInput = document.createElement('input');
            artistInput.type = 'text';
            artistInput.className = 'longform-row-input longform-artist';
            artistInput.placeholder = 'Artist';
            artistInput.value = segment.artist || '';

            const removeBtn = document.createElement('button');
            removeBtn.type = 'button';
            removeBtn.className = 'btn btn-ghost btn-sm longform-row-remove';
            removeBtn.title = 'Remove this row';
            removeBtn.innerHTML = '<i class="fa-solid fa-xmark"></i>';
            removeBtn.addEventListener('click', () => row.remove());

            row.appendChild(startInput);
            row.appendChild(endInput);
            row.appendChild(titleInput);
            row.appendChild(artistInput);
            row.appendChild(removeBtn);
            return row;
        }

        function _collectLongformSegments() {
            if (!longformSplitPanel) return [];
            const rows = longformSplitPanel.querySelectorAll('.longform-row');
            const segments = [];
            rows.forEach(row => {
                const start = _parseTimestampInput(row.querySelector('.longform-start').value);
                const end = _parseTimestampInput(row.querySelector('.longform-end').value);
                const title = row.querySelector('.longform-title').value.trim();
                const artist = row.querySelector('.longform-artist').value.trim();
                if (!title) return;
                segments.push({ start_seconds: start, end_seconds: end, title, artist: artist || null });
            });
            return segments;
        }

        function renderLongformPanel(data, sourceUrl) {
            if (!longformSplitPanel) return;
            hideAlbumMatchPanel();
            longformDetectedContext = {
                url: sourceUrl,
                video_title: data.video_title,
                thumbnail: data.thumbnail,
                duration: data.duration,
            };
            longformSplitPanel.style.display = 'block';
            longformSplitPanel.innerHTML = '';

            const header = document.createElement('div');
            header.className = 'longform-video-header';
            if (data.thumbnail) {
                const img = document.createElement('img');
                img.className = 'longform-video-thumb';
                img.src = data.thumbnail;
                img.alt = '';
                header.appendChild(img);
            }
            const titleWrap = document.createElement('div');
            const titleEl = document.createElement('div');
            titleEl.className = 'longform-video-title';
            titleEl.textContent = data.video_title || 'Untitled';
            const viaEl = document.createElement('div');
            viaEl.className = 'longform-detected-via';
            viaEl.textContent = _longformDetectedViaLabel(data.detected_via);
            titleWrap.appendChild(titleEl);
            titleWrap.appendChild(viaEl);
            header.appendChild(titleWrap);
            longformSplitPanel.appendChild(header);

            const artistRow = document.createElement('div');
            artistRow.className = 'longform-field-row';
            const artistLabel = document.createElement('span');
            artistLabel.textContent = 'Album Artist:';
            const artistInput = document.createElement('input');
            artistInput.type = 'text';
            artistInput.id = 'longformAlbumArtist';
            artistInput.className = 'longform-row-input';
            artistInput.style.maxWidth = '260px';
            const segArtists = new Set((data.segments || []).map(s => (s.artist || '').trim()).filter(Boolean));
            artistInput.value = segArtists.size === 1 ? [...segArtists][0] : 'Various Artists';
            artistRow.appendChild(artistLabel);
            artistRow.appendChild(artistInput);
            longformSplitPanel.appendChild(artistRow);

            const rowsWrap = document.createElement('div');
            rowsWrap.className = 'longform-rows-wrap';
            const segments = (data.segments && data.segments.length)
                ? data.segments
                : [{ start_seconds: 0, end_seconds: data.duration || 0, title: '', artist: '' }];
            for (const seg of segments) {
                rowsWrap.appendChild(_buildLongformRow(seg));
            }
            longformSplitPanel.appendChild(rowsWrap);

            const addRowBtn = document.createElement('button');
            addRowBtn.type = 'button';
            addRowBtn.className = 'btn btn-ghost btn-sm';
            addRowBtn.textContent = '+ Add row';
            addRowBtn.addEventListener('click', () => {
                rowsWrap.appendChild(_buildLongformRow({ start_seconds: 0, end_seconds: 0, title: '', artist: '' }));
            });
            longformSplitPanel.appendChild(addRowBtn);

            const pasteWrap = document.createElement('div');
            pasteWrap.className = 'longform-paste-wrap';
            const pasteLabel = document.createElement('p');
            pasteLabel.className = 'bulk-intro-text';
            pasteLabel.textContent = 'Or paste your own cue sheet (one "0:00 Track Title" per line) to replace the rows above:';
            const pasteTextarea = document.createElement('textarea');
            pasteTextarea.className = 'longform-paste-textarea';
            pasteTextarea.placeholder = '0:00 Track One\n3:45 Track Two';
            const pasteBtn = document.createElement('button');
            pasteBtn.type = 'button';
            pasteBtn.className = 'btn btn-ghost btn-sm';
            pasteBtn.textContent = 'Use this cue sheet';
            pasteBtn.addEventListener('click', () => {
                const parsed = _parseCueSheetTextJs(pasteTextarea.value, data.duration || 0);
                if (!parsed.length) {
                    showToast('Could not find any cue points in that text', true);
                    return;
                }
                rowsWrap.innerHTML = '';
                for (const seg of parsed) rowsWrap.appendChild(_buildLongformRow(seg));
            });
            pasteWrap.appendChild(pasteLabel);
            pasteWrap.appendChild(pasteTextarea);
            pasteWrap.appendChild(pasteBtn);
            longformSplitPanel.appendChild(pasteWrap);

            const actions = document.createElement('div');
            actions.className = 'longform-actions';
            const confirmBtn = document.createElement('button');
            confirmBtn.type = 'button';
            confirmBtn.className = 'btn btn-primary';
            confirmBtn.textContent = 'Split & Download';
            confirmBtn.addEventListener('click', () => confirmLongformSplit(confirmBtn));
            const cancelBtn = document.createElement('button');
            cancelBtn.type = 'button';
            cancelBtn.className = 'btn btn-ghost';
            cancelBtn.textContent = 'Cancel';
            cancelBtn.addEventListener('click', hideLongformPanel);
            actions.appendChild(confirmBtn);
            actions.appendChild(cancelBtn);
            longformSplitPanel.appendChild(actions);
        }

        async function confirmLongformSplit(btnEl) {
            if (!longformDetectedContext) return;
            const segments = _collectLongformSegments();
            if (!segments.length) {
                showToast('Add at least one track with a title first', true);
                return;
            }
            const albumArtistInput = document.getElementById('longformAlbumArtist');
            const albumArtist = (albumArtistInput && albumArtistInput.value.trim()) || 'Various Artists';

            if (btnEl) { btnEl.disabled = true; btnEl.textContent = 'Starting…'; }
            try {
                const response = await apiFetch('/api/longform/split', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        url: longformDetectedContext.url,
                        video_title: longformDetectedContext.video_title,
                        album_artist: albumArtist,
                        segments,
                    }),
                });
                const data = await response.json().catch(() => ({}));
                if (!response.ok) {
                    throw new Error(data.detail || 'Could not start the split');
                }
                showLongformSplitProgress({
                    status: 'pending', total_segments: data.total_segments,
                    completed_segments: 0, failed_segments: 0,
                    video_title: longformDetectedContext.video_title, complete: false,
                }, data.split_id);
                startLongformSplitPolling(data.split_id);
                spotifyUrlInput.value = '';
            } catch (error) {
                showToast(error.message || 'Could not start the split', true);
                if (btnEl) { btnEl.disabled = false; btnEl.textContent = 'Split & Download'; }
            }
        }

        function startLongformSplitPolling(splitId) {
            if (longformSplitPollInterval) {
                clearInterval(longformSplitPollInterval);
            }
            longformSplitPollInterval = setInterval(async () => {
                try {
                    const response = await apiFetch(`/api/longform/${splitId}/status`);
                    if (!response.ok) throw new Error('Failed to get status');
                    const data = await response.json();
                    showLongformSplitProgress(data, splitId);
                    if (data.complete) {
                        clearInterval(longformSplitPollInterval);
                        longformSplitPollInterval = null;
                    }
                } catch (error) {
                    console.error('Long-form split polling error:', error);
                }
            }, 2000);
        }

        function showLongformSplitProgress(data, splitId) {
            if (!longformSplitPanel) return;
            longformSplitPanel.style.display = 'block';
            longformSplitPanel.innerHTML = '';

            const total = data.total_segments || 0;
            const done = (data.completed_segments || 0) + (data.failed_segments || 0);
            const percent = total > 0 ? Math.round((done / total) * 100) : 0;

            let statusText = 'Downloading and splitting…';
            let statusColor = 'var(--accent)';
            if (data.status === 'completed') {
                statusText = 'Complete';
            } else if (data.status === 'completed_with_errors') {
                statusText = 'Complete, with some failures';
                statusColor = 'var(--warning)';
            } else if (data.status === 'failed') {
                statusText = data.error || 'Failed';
                statusColor = 'var(--error)';
            }

            const wrap = document.createElement('div');
            wrap.innerHTML = `
                <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
                    <div style="font-weight:600;">${escapeHtml(data.video_title || 'Splitting video')}</div>
                    <div style="font-size:12px;color:${statusColor};">${escapeHtml(statusText)}</div>
                </div>
                <div style="background: var(--bg-tertiary); border-radius: 4px; height: 8px; margin-bottom: 10px; overflow: hidden;">
                    <div style="background: ${statusColor}; height: 100%; width: ${percent}%; transition: width 0.3s ease;"></div>
                </div>
                <div style="font-size:12px;color:var(--text-secondary);">${done} / ${total} segment${total === 1 ? '' : 's'} processed</div>
            `;
            longformSplitPanel.appendChild(wrap);

            if (!data.complete && splitId) {
                const cancelBtn = document.createElement('button');
                cancelBtn.type = 'button';
                cancelBtn.className = 'btn btn-ghost btn-sm';
                cancelBtn.style.marginTop = '8px';
                cancelBtn.textContent = 'Cancel';
                cancelBtn.addEventListener('click', async () => {
                    cancelBtn.disabled = true;
                    try {
                        await apiFetch(`/api/longform/${splitId}/cancel`, { method: 'POST' });
                        showToast('Cancellation requested. The current segment may finish.');
                    } catch (error) {
                        showToast('Could not cancel', true);
                    }
                });
                longformSplitPanel.appendChild(cancelBtn);
            } else if (data.complete) {
                const dismissBtn = document.createElement('button');
                dismissBtn.type = 'button';
                dismissBtn.className = 'btn btn-ghost btn-sm';
                dismissBtn.style.marginTop = '8px';
                dismissBtn.textContent = 'Dismiss';
                dismissBtn.addEventListener('click', hideLongformPanel);
                longformSplitPanel.appendChild(dismissBtn);
            }
        }

        // Only the Bulk Import Fetch box triggers long-form detection - never
        // Search, never bulk-textarea lines. Returns true when the panel took
        // over (caller should stop), false when the caller should fall through
        // to its normal handling for this URL.
        async function tryLongformDetect(url) {
            fetchSpotifyBtn.disabled = true;
            fetchSpotifyBtn.textContent = 'Checking video…';
            const slowHint = setTimeout(() => {
                fetchSpotifyBtn.textContent = 'Hunting for a cue sheet…';
            }, 4000);
            try {
                const response = await apiFetch('/api/longform/detect', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ url }),
                });
                const data = await response.json().catch(() => ({}));
                if (!response.ok || !data.eligible) {
                    return false;
                }
                spotifyError.style.display = 'none';
                renderLongformPanel(data, url);
                return true;
            } catch (error) {
                return false;
            } finally {
                clearTimeout(slowHint);
                fetchSpotifyBtn.disabled = false;
                fetchSpotifyBtn.textContent = 'Fetch';
            }
        }

        // The ordinary playlist scrape (/api/fetch-playlist), factored out of
        // fetchSpotifyPlaylist so both the normal routing path and the "turned
        // out not to be an album after all" fallback from fetchAlbumUrl can
        // call it without duplicating the button-state dance.
        async function doPlaylistFetch(url, isAmazon) {
            spotifyError.style.display = 'none';
            fetchSpotifyBtn.disabled = true;
            fetchSpotifyBtn.textContent = isAmazon ? 'Scraping...' : 'Fetching...';

            // For Spotify, the embed scrape returns in seconds; if the playlist
            // is large or private, the server falls back to a headless browser
            // which takes longer. Nudge the button text after a few seconds
            // so the user knows we haven't ghosted them.
            const isSpotify = /^https?:\/\/open\.spotify\.com\//.test(url);
            const slowHints = [];
            if (isSpotify) {
                slowHints.push(setTimeout(() => {
                    fetchSpotifyBtn.textContent = 'Spinning up browser...';
                }, 6000));
                slowHints.push(setTimeout(() => {
                    fetchSpotifyBtn.textContent = 'Scrolling playlist (large lists take a minute)...';
                }, 15000));
            }

            try {
                const response = await apiFetch('/api/fetch-playlist', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ url })
                });

                // Check content type before parsing
                const contentType = response.headers.get('content-type');
                if (!contentType || !contentType.includes('application/json')) {
                    throw new Error('Server error - please check if the app is running');
                }

                const data = await response.json();

                if (!response.ok) {
                    if (data.detail === 'spotify_cookies_expired') {
                        throw new Error('Spotify cookies have expired. Go to Settings to update them.');
                    }
                    throw new Error(data.detail || 'Failed to fetch playlist');
                }

                if (data.tracks && data.tracks.length > 0) {
                    // Populate the textarea with all tracks (no limit)
                    bulkInput.value = data.tracks.join('\n');
                    if (Array.isArray(data.original_youtube_video_ids)) {
                        bulkInput.dataset.originalYoutubeVideoMap = JSON.stringify(
                            data.tracks.map((track, index) => ({
                                track,
                                video_id: data.original_youtube_video_ids[index] || null
                            }))
                        );
                    } else {
                        delete bulkInput.dataset.originalYoutubeVideoMap;
                    }
                    updateLineCounter();

                    // Auto-fill playlist name if checkbox is checked
                    if (createPlaylistCheckbox.checked && data.playlist_name) {
                        playlistNameInput.value = data.playlist_name;
                        playlistNameInput.dataset.sourceUrl = url;
                    }

                    // For SoundCloud playlists, default source chips to SC only - the tracks
                    // are already on SoundCloud, so there's no reason to go hunting elsewhere
                    if (data.platform === 'soundcloud') {
                        setWatchedPreferredSource('soundcloud');
                    }

                    // Show toast with result (include warning if present)
                    if (data.warning) {
                        spotifyError.textContent = `Warning: ${data.warning}`;
                        spotifyError.style.color = 'var(--warning, #f59e0b)';
                        spotifyError.style.display = 'block';
                        showToast(`Loaded ${data.tracks.length} tracks (truncated) - ${data.warning}`, true);
                    } else {
                        showToast(`Loaded ${data.tracks.length} tracks from "${data.playlist_name}"`);
                    }

                    // Clear the URL input
                    spotifyUrlInput.value = '';
                } else {
                    throw new Error('No tracks found in playlist');
                }
                return true;
            } catch (error) {
                spotifyError.textContent = error.message;
                spotifyError.style.color = 'var(--error)';
                spotifyError.style.display = 'block';
                return false;
            } finally {
                slowHints.forEach(clearTimeout);
                fetchSpotifyBtn.disabled = false;
                fetchSpotifyBtn.textContent = 'Fetch';
            }
        }

        // Single input, works out what it was given: playlist URL, MusicBrainz
        // release URL, Spotify/Apple album URL, or plain text to search
        // MusicBrainz for by name. This is the whole point of unifying Bulk
        // Import; nobody should have to pick from a dropdown before they've
        // even typed anything.
        async function fetchSpotifyPlaylist() {
            const url = spotifyUrlInput.value.trim();
            if (!url) {
                spotifyError.textContent = 'Please enter a URL, or an album name to search';
                spotifyError.style.display = 'block';
                return;
            }
            hideLongformPanel();

            // Long-form YouTube splitting: only for a single video, so it never
            // touches a genuine curated playlist (list=PL.../OLAK5uy...). A bare
            // watch URL (no list=) or YouTube's own auto-generated "RD..." radio
            // list both qualify - RD gets appended to normal watch URLs and isn't
            // something a user deliberately curated. When the video turns out
            // not to be a split candidate (too short, or lookup failed), this
            // falls straight through to the unchanged behaviour below: a bare
            // watch URL hits "Unsupported URL", an RD-list URL goes through the
            // ordinary playlist-import path.
            if (/^https?:\/\/(?:www\.|music\.)?youtube\.com\/watch\?/i.test(url)) {
                let listParam = null;
                try {
                    listParam = new URL(url).searchParams.get('list');
                } catch (e) { /* not a parseable absolute URL; fall through */ }
                const isRealPlaylist = listParam && !listParam.startsWith('RD');
                if (!isRealPlaylist) {
                    const handled = await tryLongformDetect(url);
                    if (handled) return;
                }
            }

            // URL validation - Spotify playlists/albums, Amazon Music playlists, Apple Music, YouTube/YT Music playlists, SoundCloud sets/likes, ListenBrainz, Tidal/Monochrome, Beatport
            const isSpotifyAlbum = url.match(/^https?:\/\/open\.spotify\.com\/album\//);
            const isSpotify = isSpotifyAlbum || url.match(/^https?:\/\/open\.spotify\.com\/playlist\//);
            const isAmazon = url.match(/^https?:\/\/music\.amazon\.[a-z.]+\/(user-playlists|playlists)\//);
            const isAppleAlbum = url.match(/^https?:\/\/music\.apple\.com\/(?:[a-z]{2}|library)\/album\//i);
            const isApple = isAppleAlbum || url.match(/^https?:\/\/music\.apple\.com\/(?:[a-z]{2}|library)\/playlist\//i);
            const isYouTube = url.match(/^https?:\/\/(www\.|music\.)?youtube\.com\/(playlist|watch)\?[^"]*list=/i);
            const isSoundCloud = url.match(/^https?:\/\/soundcloud\.com\/[^/]+\/(sets\/[^/?]+|likes)/i);
            // The bare "^[a-zA-Z0-9_-]+$" alternative is a bare ListenBrainz
            // username (no URL at all) - existing behaviour. It happens to also
            // match a one-word album title ("Trainspotting"), which is exactly
            // the ambiguity handled below: try ListenBrainz first, and only if
            // that comes back empty does it get treated as an album search.
            const isListenBrainzUrl = url.match(/^https?:\/\/listenbrainz\.org\/(playlist|user)\//i);
            const isBareWord = url.match(/^[a-zA-Z0-9_-]+$/);
            const isListenBrainz = isListenBrainzUrl || isBareWord;
            const isTidalAlbum = url.match(/^https?:\/\/(?:www\.)?tidal\.com\/(?:browse\/)?album\/\d+/i);
            const isTidal = isTidalAlbum || url.match(/^https?:\/\/tidal\.com\/(?:browse\/)?playlist\//i);
            const isMonochromeAlbum = url.match(/^https?:\/\/(?:www\.)?(?:monochrome\.tf|monochrome\.samidy\.com)\/album\/\d+/i);
            const isMonochrome = isMonochromeAlbum || url.match(/^https?:\/\/(?:www\.)?(?:monochrome\.tf|monochrome\.samidy\.com)\/playlist\//i);
            // Beatport calls an album a "release"; charts and Top 100 are not albums.
            const isBeatportRelease = url.match(/^https?:\/\/(?:www\.)?beatport\.com\/release\/[^/?#]+\/\d+/i);
            const isBeatport = isBeatportRelease || url.match(/^https?:\/\/(?:www\.)?beatport\.com\/(top-100|genre\/[^/]+\/\d+\/top-100|chart\/[^/]+\/\d+)/i);
            // YouTube's auto-generated album playlists are the OLAK5uy_ ones. An
            // ordinary PL... playlist or an RD... mix is not an album.
            const isYouTubeAlbum = url.match(/^https?:\/\/(?:www\.|music\.)?youtube\.com\/(?:playlist|watch)\?[^"]*list=OLAK5uy_/i);
            // MusicBrainz release/release-group URLs carry the MBID already, so they
            // skip the track-list textarea entirely and go straight to the proper
            // album pipeline: Albums/Artist/Album/, cover art, the works.
            const isMusicBrainz = url.match(/^https?:\/\/(?:beta\.)?musicbrainz\.org\/(release|release-group)\/[0-9a-f-]{36}/i);
            const looksLikeUrl = /^https?:\/\//i.test(url);

            if (isMusicBrainz) {
                await fetchMusicBrainzRelease(url);
                return;
            }
            // ALBUM url on any supported service: resolve-album-url first
            // (confidence gate), NOT the ordinary playlist scrape, or the whole
            // album flattens into a track list and loses its
            // folder/artwork/.albuminfo/track numbers.
            if (isSpotifyAlbum || isAppleAlbum || isYouTubeAlbum || isBeatportRelease
                || isTidalAlbum || isMonochromeAlbum) {
                await fetchAlbumUrl(url);
                return;
            }
            if (!isSpotify && !isAmazon && !isApple && !isYouTube && !isSoundCloud && !isListenBrainz && !isTidal && !isMonochrome && !isBeatport) {
                if (!looksLikeUrl) {
                    // No scheme at all, and not a bare ListenBrainz-shaped word:
                    // this is somebody typing an album (or "Artist - Album") name.
                    await searchReleaseByName(url);
                    return;
                }
                spotifyError.textContent = 'Unsupported URL. Paste a Spotify, YouTube, Apple Music, Amazon Music, SoundCloud sets/likes, ListenBrainz, Tidal, Monochrome, MusicBrainz release, or Beatport link, or type an artist/album name to search.';
                spotifyError.style.display = 'block';
                return;
            }

            const ok = await doPlaylistFetch(url, isAmazon);
            if (!ok && isBareWord && !isListenBrainzUrl) {
                // The bare word didn't resolve as a ListenBrainz username (blame
                // ListenBrainz for having such relaxed rules on those); it is
                // far more likely to be a one-word album title, so have a go at
                // that instead, quietly, rather than leaving the user staring
                // at a ListenBrainz error for an album search.
                await searchReleaseByName(url);
            }
        }

        fetchSpotifyBtn.addEventListener('click', fetchSpotifyPlaylist);
        spotifyUrlInput.addEventListener('keypress', (e) => {
            if (e.key === 'Enter') {
                fetchSpotifyPlaylist();
            }
        });

        // Show/hide playlist name input (and Playlists folder checkbox) when checkbox is toggled
        createPlaylistCheckbox.addEventListener('change', () => {
            playlistNameInput.style.display = createPlaylistCheckbox.checked ? 'block' : 'none';
            const playlistsDirRow = document.getElementById('usePlaylistsDirRow');
            const usePlaylistsDirCheckbox = document.getElementById('usePlaylistsDirCheckbox');
            if (playlistsDirRow) {
                playlistsDirRow.style.display = (createPlaylistCheckbox.checked && serverConfig.playlists_subdir) ? 'flex' : 'none';
            }
            if (usePlaylistsDirCheckbox && createPlaylistCheckbox.checked && serverConfig.playlists_subdir) {
                // Default playlist-creation imports to the Playlists folder when configured.
                usePlaylistsDirCheckbox.checked = true;
            }
            if (createPlaylistCheckbox.checked) {
                playlistNameInput.focus();
            }
        });

        // Close search history when clicking outside
        document.addEventListener('click', (e) => {
            if (!searchInput.contains(e.target) && !searchHistory.contains(e.target)) {
                searchHistory.classList.remove('show');
            }
        });

        // Initial empty state
        resultsTab.innerHTML = `
            <div class="empty-state">
                <div class="empty-state-icon">~</div>
                <p>Search for music to get started</p>
                <p style="font-size: 12px; margin-top: 8px; opacity: 0.6;">Hover over results to preview</p>
            </div>
        `;

        // =============================================================================
        // Watched Playlists
        // =============================================================================

        async function loadWatchedPlaylists(showLoading = true) {
            if (watchedLoadInFlight) return;
            watchedLoadInFlight = true;
            if (showLoading) {
                watchedList.innerHTML = '<div class="loading"><div class="spinner"></div></div>';
            }

            try {
                // Fetch schedule info and playlists in parallel
                const [scheduleRes, playlistsRes] = await Promise.all([
                    apiFetch('/api/watched-playlists/schedule'),
                    apiFetch('/api/watched-playlists')
                ]);

                // Update schedule info
                if (scheduleRes.ok) {
                    const schedule = await scheduleRes.json();
                    if (schedule.enabled) {
                        const hours = schedule.check_interval_hours;
                        let intervalText = `${hours} hours`;
                        if (hours === 24) intervalText = 'daily';
                        else if (hours === 168) intervalText = 'weekly';
                        else if (hours >= 720) intervalText = 'monthly';
                        watchedScheduleInfo.textContent = `Automatic checks follow each playlist's own interval. The global ${intervalText} setting is only the maximum time between scheduler sweeps.`;
                    } else {
                        watchedScheduleInfo.textContent = 'Automatic checks disabled. Use "Check All Now" or set WATCHED_PLAYLIST_CHECK_HOURS.';
                    }
                }

                if (!playlistsRes.ok) throw new Error('Failed to load watched playlists');

                const data = await playlistsRes.json();
                renderWatchedPlaylists(data.playlists);
                populateSourceChips();
            } catch (error) {
                if (showLoading) {
                    watchedList.innerHTML = `
                        <div class="empty-state">
                            <p style="color: var(--error);">Failed to load watched playlists</p>
                        </div>
                    `;
                }
            } finally {
                watchedLoadInFlight = false;
            }
        }

        function renderWatchedPlaylists(playlists) {
            if (!playlists || playlists.length === 0) {
                if (watchedRefreshPollInterval) {
                    clearInterval(watchedRefreshPollInterval);
                    watchedRefreshPollInterval = null;
                }
                watchedList.innerHTML = `
                    <div class="empty-state">
                        <div class="empty-state-icon"><i class="fa-solid fa-eye"></i></div>
                        <p>No watched playlists yet</p>
                        <p style="font-size: 12px; margin-top: 8px; opacity: 0.6;">Add a Spotify or YouTube playlist above</p>
                    </div>
                `;
                return;
            }

            watchedList.innerHTML = playlists.map(p => {
                const refreshState = p.refresh_state || 'idle';
                const localRefresh = watchedRefreshPending.get(p.id);
                const isRefreshing = refreshState === 'running' || !!localRefresh;
                const refreshStage = (refreshState === 'running' ? p.refresh_stage : null) || (localRefresh ? localRefresh.stage : null);
                const refreshStartedAt = (refreshState === 'running' ? p.refresh_started_at : null)
                    || (localRefresh ? localRefresh.startedAt : null);
                const platformIcons = {
                    spotify: '<i class="fa-brands fa-spotify" title="Spotify"></i>',
                    youtube: '<i class="fa-brands fa-youtube" title="YouTube"></i>',
                    apple: '<i class="fa-brands fa-apple" title="Apple Music"></i>',
                    amazon: '<i class="fa-brands fa-amazon" title="Amazon Music"></i>',
                    listenbrainz: '<img src="/static/images/ListenBrainzLogo.svg" title="ListenBrainz" style="width:1em;height:1em;vertical-align:-0.125em;">',
                    soundcloud: '<i class="fa-brands fa-soundcloud" title="SoundCloud"></i>',
                    tidal: '<img src="/static/images/tidal-round-black-icon.svg" title="Tidal" style="width:1em;height:1em;vertical-align:-0.125em;">',
                    monochrome: '<span title="Monochrome" style="font-size:0.75em;font-weight:700;">MONO</span>',
                    beatport: '<img src="/static/images/BeatPortLogo.svg" title="Beatport" style="width:1em;height:1em;vertical-align:-0.125em;">'
                };
                const platformIcon = platformIcons[p.platform] || '<i class="fa-solid fa-list"></i>';
                const lastChecked = p.last_checked ? formatTimeAgo(p.last_checked) : 'Never';
                const lastCheckedExact = p.last_checked ? formatTimeExact(p.last_checked) : '';
                const statusColor = p.enabled ? 'var(--accent)' : 'var(--text-secondary)';
                const intervalText = p.refresh_interval_hours >= 720 ? 'monthly' :
                                     p.refresh_interval_hours === 168 ? 'weekly' :
                                     p.refresh_interval_hours === 24 ? 'daily' :
                                     p.refresh_interval_hours === 12 ? 'every 12h' :
                                     p.refresh_interval_hours === 6 ? 'every 6h' :
                                     p.refresh_interval_hours === 1 ? 'hourly' :
                                     p.refresh_interval_hours === 0.5 ? 'every 30min' :
                                     `every ${p.refresh_interval_hours}h`;
                const refreshLabel = isRefreshing
                    ? formatRefreshStage(refreshStage, refreshStartedAt, p.platform)
                    : '';
                // Tracks that have left the upstream playlist. Mirror mode has stopped
                // listing them, append mode is keeping them on purpose; either way they
                // are not part of "how big is this playlist", which is what the first
                // two figures answer.
                const departedCount = p.departed_count || 0;
                const departedText = departedCount
                    ? ` &middot; ${departedCount} ${p.sync_mode === 'mirror' ? 'no longer upstream' : 'kept from earlier'}`
                    : '';

                return `
                    <div class="watched-card">
                        <div class="watched-card-header">
                            <span>${platformIcon}</span>
                            <span class="watched-card-name">${escapeHtml(p.name)}</span>
                            ${isRefreshing ? `<span class="watched-card-refreshing"><span class="watched-refresh-spinner"></span>${escapeHtml(refreshLabel)}</span>` : ''}
                            ${!p.enabled ? '<span class="watched-card-paused">Paused</span>' : ''}
                        </div>
                        <div class="watched-card-meta">
                            ${p.tracked_count} tracks · ${p.downloaded_count || 0} downloaded${departedText} · ${intervalText} · Last checked: <span title="${escapeAttr(lastCheckedExact)}">${escapeHtml(lastChecked)}</span>
                        </div>
                        ${refreshState === 'error' && p.refresh_error && !isRefreshing ? `
                        <div class="watched-card-refresh-error">
                            <i class="fa-solid fa-circle-exclamation"></i>
                            <span>${escapeHtml(p.refresh_error)}</span>
                        </div>` : ''}
                        <div class="watched-card-settings">
                            <label class="watched-card-toggle" title="Choose whether new tracks keep their source format or are converted">
                                ${conversionChoiceLabel(p.convert_audio)}
                                <div class="toggle-switch">
                                    <input type="checkbox" ${p.convert_audio ? 'checked' : ''} onchange="updateWatchedPlaylistConversion('${p.id}', this.checked)">
                                    <span class="toggle-slider"></span>
                                </div>
                            </label>
                            <label class="watched-card-toggle" title="Generate and update a .m3u playlist file as tracks are downloaded. Switching this off stops MusicGrabber updating it; any .m3u already written stays where it is, for you to keep or delete.">
                                M3U
                                <div class="toggle-switch">
                                    <input type="checkbox" ${p.make_m3u ? 'checked' : ''} onchange="updateWatchedPlaylistM3u('${p.id}', this.checked)">
                                    <span class="toggle-slider"></span>
                                </div>
                            </label>
                            ${serverConfig.playlists_subdir ? `
                            <label class="watched-card-toggle" title="Save downloaded tracks to the Playlists folder instead of Singles">
                                Playlists folder
                                <div class="toggle-switch">
                                    <input type="checkbox" ${p.use_playlists_dir ? 'checked' : ''} onchange="updateWatchedPlaylistUsePlaylists('${p.id}', this.checked)">
                                    <span class="toggle-slider"></span>
                                </div>
                            </label>` : ''}
                            <label class="watched-card-toggle" title="Append: M3U grows as new tracks arrive. Mirror: M3U stays in sync with upstream - removed tracks drop out (audio files kept).">
                                Sync
                                <select onchange="updateWatchedPlaylistSyncMode('${p.id}', this.value)" class="watched-card-select">
                                    <option value="append" ${(p.sync_mode || 'append') === 'append' ? 'selected' : ''}>Append</option>
                                    <option value="mirror" ${p.sync_mode === 'mirror' ? 'selected' : ''}>Mirror</option>
                                </select>
                            </label>
                            <label class="watched-card-toggle" title="Choose whether this playlist inherits the global missing-file policy, re-downloads vanished files, or leaves downloads alone after an external organiser moves them.">
                                Missing files
                                <select onchange="updateWatchedPlaylistRedownloadMissing('${p.id}', this.value)" class="watched-card-select">
                                    <option value="inherit" ${p.redownload_missing_files == null ? 'selected' : ''}>Global setting</option>
                                    <option value="true" ${p.redownload_missing_files === 1 || p.redownload_missing_files === true ? 'selected' : ''}>Re-download</option>
                                    <option value="false" ${p.redownload_missing_files === 0 || p.redownload_missing_files === false ? 'selected' : ''}>Leave alone</option>
                                </select>
                            </label>
                            <label class="watched-card-toggle" title="Which search sources to use when downloading new tracks for this playlist. Deselect all to search everything.">
                                Sources
                                <div class="watched-sources-chips" data-playlist-id="${p.id}" data-preferred="${escapeAttr(p.preferred_sources || 'all')}">
                                    ${renderSourceChips(p.id, p.preferred_sources || 'all')}
                                </div>
                            </label>
                            <label class="watched-card-toggle" title="Break ties in favour of this source within the same quality tier. Allowed lossless results still come before lossy ones.">
                                Preferred
                                <select onchange="updateWatchedPlaylistPrioritySource('${p.id}', this.value)" class="watched-card-select" data-priority-source="${escapeAttr(p.priority_source || '')}">
                                    ${renderPrioritySourceOptions(p.priority_source || '')}
                                </select>
                            </label>
                        </div>
                        ${p.unresolved_tracks > 0 ? `
                        <div class="watched-card-stale-warning">
                            <i class="fa-solid fa-circle-info"></i>
                            <span><strong>${p.unresolved_tracks} downloaded track${p.unresolved_tracks === 1 ? '' : 's'}</strong> can no longer be found on disk. This playlist's missing-file policy is leaving them alone rather than fetching duplicates, so they will not appear in the M3U. This is what you want if another tool has moved or renamed them; if you deleted them on purpose, use the Missing panel's Retry to fetch one back.</span>
                        </div>` : ''}
                        ${p.stale_navidrome_paths > 0 ? `
                        <div class="watched-card-stale-warning">
                            <i class="fa-solid fa-triangle-exclamation"></i>
                            <span><strong>${p.stale_navidrome_paths} track${p.stale_navidrome_paths === 1 ? '' : 's'}</strong> in the M3U point to files that no longer exist on disk but are still in Navidrome's database. To fix: open Navidrome, go to <strong>Settings &gt; Missing Files</strong>, select all, and click <strong>Remove from Database</strong>. Then trigger a library scan and refresh this playlist.</span>
                        </div>` : ''}
                        <div class="watched-card-actions">
                            <button type="button"
                                data-action="refresh-watched-playlist"
                                data-playlist-id="${escapeAttr(p.id)}"
                                class="watched-action-btn" title="${isRefreshing ? `Refresh in progress: ${escapeAttr(refreshLabel)}` : 'Check for new tracks now'}" ${isRefreshing ? 'disabled' : ''}>${isRefreshing ? 'Checking...' : 'Refresh'}</button>
                            <button type="button"
                                data-action="toggle-missing-tracks"
                                data-playlist-id="${escapeAttr(p.id)}"
                                class="watched-action-btn" title="Show tracks that failed to download">Missing</button>
                            <button type="button"
                                data-action="toggle-playlist-track-list"
                                data-playlist-id="${escapeAttr(p.id)}"
                                data-playlist-name="${escapeAttr(p.name)}"
                                class="watched-action-btn" title="Show all tracks and their download status">Tracks</button>
                            <button type="button"
                                data-action="copy-watched-playlist-url"
                                data-url="${escapeAttr(p.url)}"
                                class="watched-action-btn" title="Copy playlist URL">Copy URL</button>
                            <button type="button"
                                data-action="toggle-watched-playlist"
                                data-playlist-id="${escapeAttr(p.id)}"
                                data-enabled="${p.enabled ? 'true' : 'false'}"
                                class="watched-action-btn watched-action-pause">${p.enabled ? 'Pause' : 'Resume'}</button>
                            <button type="button"
                                data-action="delete-watched-playlist"
                                data-playlist-id="${escapeAttr(p.id)}"
                                data-playlist-name="${escapeAttr(p.name)}"
                                class="watched-action-btn watched-action-delete">Delete</button>
                        </div>
                        <div id="missing-${p.id}" class="watched-card-expanded" style="display: none;">Loading...</div>
                        <div id="tracks-${p.id}" class="watched-card-expanded" style="display: none;">Loading...</div>
                    </div>
                `;
            }).join('');

            const anyRunning = playlists.some(p => p.refresh_state === 'running') || watchedRefreshPending.size > 0;
            if (currentTab === 'watched' && anyRunning && !watchedRefreshPollInterval) {
                watchedRefreshPollInterval = setInterval(() => {
                    loadWatchedPlaylists(false);
                }, 2000);
            } else if ((!anyRunning || currentTab !== 'watched') && watchedRefreshPollInterval) {
                clearInterval(watchedRefreshPollInterval);
                watchedRefreshPollInterval = null;
            }
        }

        function formatRefreshStage(stage, startedAt = null, platform = null) {
            const labels = {
                starting: 'Starting...',
                fetching: 'Fetching playlist...',
                diffing: 'Comparing tracks...',
                queueing: 'Queueing downloads...',
                finalizing: 'Finalizing...',
                rebuilding_m3u: 'Rebuilding M3U...',
                done: 'Done',
                failed: 'Failed'
            };
            const base = !stage ? 'Checking...' : (labels[stage] || stage.replace(/_/g, ' '));

            if (!startedAt) return base;
            const normalized = /Z$|[+-]\d{2}:\d{2}$/.test(startedAt) ? startedAt : `${String(startedAt).replace(' ', 'T')}Z`;
            const startedMs = new Date(normalized).getTime();
            if (Number.isNaN(startedMs)) return base;
            const elapsedSec = Math.max(0, Math.floor((Date.now() - startedMs) / 1000));
            const mins = Math.floor(elapsedSec / 60);
            const secs = elapsedSec % 60;
            const elapsedText = mins > 0 ? `${mins}m ${String(secs).padStart(2, '0')}s` : `${secs}s`;

            if (stage === 'fetching' && platform === 'spotify' && elapsedSec >= 90) {
                return `${base} ${elapsedText} (large playlists can take a few minutes)`;
            }
            return `${base} ${elapsedText}`;
        }

        function formatTimeAgo(isoString) {
            if (!isoString) return '';
            const normalized = /Z$|[+-]\d{2}:\d{2}$/.test(isoString) ? isoString : `${String(isoString).replace(' ', 'T')}Z`;
            const date = new Date(normalized);
            if (Number.isNaN(date.getTime())) return '';
            const seconds = Math.max(0, Math.floor((Date.now() - date.getTime()) / 1000));
            if (seconds < 60) return 'just now';
            if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
            if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
            if (seconds < 604800) return `${Math.floor(seconds / 86400)}d ago`;
            return formatDateYmd(isoString);
        }

        function formatTimeExact(isoString) {
            if (!isoString) return '';
            const normalized = /Z$|[+-]\d{2}:\d{2}$/.test(isoString) ? isoString : `${String(isoString).replace(' ', 'T')}Z`;
            const date = new Date(normalized);
            return Number.isNaN(date.getTime()) ? '' : date.toLocaleString();
        }

        async function addWatchedPlaylist() {
            const url = watchedUrlInput.value.trim();
            if (!url) {
                watchedError.textContent = 'Please enter a playlist URL';
                watchedError.style.display = 'block';
                return;
            }

            // Detect platform so we can show helpful hints (e.g. Spotify large playlist warning)
            let platform = null;
            if (url.includes('spotify.com')) platform = 'spotify';
            else if (url.includes('youtube.com') || url.includes('youtu.be')) platform = 'youtube';
            else if (url.includes('music.apple.com')) platform = 'apple';
            else if (url.includes('amazon.') || url.includes('music.amazon')) platform = 'amazon';

            watchedError.style.display = 'none';
            addWatchedBtn.disabled = true;
            addWatchedBtn.textContent = 'Watch';

            const statusEl = document.getElementById('watchedAddStatus');
            const startedAt = new Date().toISOString();
            let timerInterval = null;

            function updateStatus(stage) {
                statusEl.innerHTML = `<span class="watched-card-refreshing"><span class="watched-refresh-spinner"></span>${escapeHtml(formatRefreshStage(stage, startedAt, platform))}</span>`;
                statusEl.style.display = 'block';
            }

            // Tick the elapsed timer every second while in-flight
            updateStatus('fetching');
            timerInterval = setInterval(() => updateStatus('fetching'), 1000);

            try {
                const response = await apiFetch('/api/watched-playlists', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        url: url,
                        refresh_interval_hours: parseFloat(watchedIntervalSelect.value),
                        convert_audio: watchedConvertAudio ? watchedConvertAudio.checked : convertAudioCheckbox.checked,
                        make_m3u: document.getElementById('watchedMakeM3u') ? document.getElementById('watchedMakeM3u').checked : false,
                        use_playlists_dir: document.getElementById('watchedUsePlaylistsDir') ? document.getElementById('watchedUsePlaylistsDir').checked : false,
                        sync_mode: document.getElementById('watchedSyncModeSelect') ? document.getElementById('watchedSyncModeSelect').value : 'append',
                        preferred_sources: getWatchedPreferredSources(),
                        priority_source: (document.getElementById('watchedPrioritySource')?.value || '') || null,
                        custom_subdir: (document.getElementById('watchedCustomSubdir')?.value || '').trim() || null
                    })
                });

                if (!response.ok) {
                    const ct = response.headers.get('content-type') || '';
                    if (ct.includes('application/json')) {
                        const error = await response.json();
                        if (error.detail === 'spotify_cookies_expired') {
                            throw new Error('Spotify cookies have expired. Go to Settings to update them.');
                        }
                        throw new Error(error.detail || 'Failed to add playlist');
                    }
                    const text = (await response.text()).slice(0, 200);
                    throw new Error(`Server error ${response.status}: ${text || response.statusText}`);
                }

                const data = await response.json();
                if (data.warning) {
                    watchedError.textContent = `Warning: ${data.warning}`;
                    watchedError.style.color = 'var(--warning, #f59e0b)';
                    watchedError.style.display = 'block';
                    showToast(`Now watching "${data.name}" (${data.track_count} tracks, truncated)`, true);
                } else {
                    showToast(`Now watching "${data.name}" (${data.track_count} tracks)`);
                }
                watchedUrlInput.value = '';
                const customSubdirInput = document.getElementById('watchedCustomSubdir');
                if (customSubdirInput) { customSubdirInput.value = ''; clearWatchedCustomSubdir(); }
                loadWatchedPlaylists();
            } catch (error) {
                watchedError.textContent = error.message;
                watchedError.style.color = 'var(--error)';
                watchedError.style.display = 'block';
            } finally {
                clearInterval(timerInterval);
                statusEl.style.display = 'none';
                addWatchedBtn.disabled = false;
                addWatchedBtn.textContent = 'Watch';
            }
        }

        function toggleLbRow() {
            const row = document.getElementById('lbRow');
            const btn = document.getElementById('lbToggleBtn');
            const visible = row.style.display !== 'none';
            row.style.display = visible ? 'none' : 'flex';
            btn.textContent = visible ? '+ ListenBrainz "Created for You" playlists' : '− ListenBrainz "Created for You" playlists';
            if (!visible) document.getElementById('lbUsernameInput').focus();
        }

        // ---------------------------------------------------------------
        // Orphaned playlist tracks (mirror-mode leftovers)
        // ---------------------------------------------------------------
        async function findPlaylistOrphans() {
            const btn = document.getElementById('findOrphansBtn');
            const panel = document.getElementById('orphansResult');
            btn.disabled = true;
            btn.textContent = 'Scanning...';
            try {
                const response = await apiFetch('/api/watched-playlists/orphans');
                if (!response.ok) throw new Error('Scan failed');
                const data = await response.json();
                renderOrphans(data.orphans || []);
            } catch (e) {
                panel.style.display = 'block';
                panel.innerHTML = `<div class="form-error">Orphan scan failed: ${escapeHtml(e.message)}</div>`;
            } finally {
                btn.disabled = false;
                btn.textContent = 'Find Orphans';
            }
        }

        function renderOrphans(orphans) {
            const panel = document.getElementById('orphansResult');
            panel.style.display = 'block';
            if (!orphans.length) {
                panel.innerHTML = '<p class="watched-panel-text" style="margin-top:10px;">No orphans found. Every file in your playlist folders is claimed by a playlist, as it should be.</p>';
                return;
            }
            const rows = orphans.map(o => `
                <div class="orphan-row" data-file="${escapeAttr(o.file)}" style="display:flex; align-items:center; gap:10px; padding:6px 0; border-bottom:1px solid var(--border);">
                    <div style="flex:1; min-width:0;">
                        <div style="overflow:hidden; text-overflow:ellipsis; white-space:nowrap;" title="${escapeAttr(o.file)}">${escapeHtml(o.name)}</div>
                        <div style="font-size:12px; color:var(--text-secondary);">${escapeHtml(o.playlist)}${o.size_mb ? ` &middot; ${o.size_mb} MB` : ''}</div>
                    </div>
                    <button class="btn" onclick="moveOrphans([this.closest('.orphan-row').dataset.file])">Move to Singles</button>
                </div>`).join('');
            panel.innerHTML = `
                <div style="display:flex; align-items:center; justify-content:space-between; margin:10px 0 4px;">
                    <strong>${orphans.length} orphaned track${orphans.length === 1 ? '' : 's'}</strong>
                    <button class="btn btn-primary" onclick="moveOrphans(Array.from(document.querySelectorAll('#orphansResult .orphan-row')).map(r => r.dataset.file))">Move All to Singles</button>
                </div>
                ${rows}`;
        }

        async function moveOrphans(files) {
            if (!files.length) return;
            try {
                const response = await apiFetch('/api/watched-playlists/orphans/move', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ files })
                });
                if (!response.ok) throw new Error('Move failed');
                const data = await response.json();
                const bits = [];
                if (data.moved.length) bits.push(`${data.moved.length} moved`);
                if (data.skipped.length) bits.push(`${data.skipped.length} skipped`);
                if (data.errors.length) bits.push(`${data.errors.length} failed`);
                showToast(`Orphans: ${bits.join(', ') || 'nothing to do'}`);
                findPlaylistOrphans(); // rescan so the list reflects reality
            } catch (e) {
                showToast(`Orphan move failed: ${e.message}`);
            }
        }

        async function addListenBrainzPlaylists() {
            const username = document.getElementById('lbUsernameInput').value.trim();
            if (!username) {
                watchedError.textContent = 'Please enter a ListenBrainz username';
                watchedError.style.display = 'block';
                return;
            }

            watchedError.style.display = 'none';
            const btn = document.getElementById('addLbBtn');
            btn.disabled = true;
            btn.textContent = 'Add';

            const statusEl = document.getElementById('watchedAddStatus');
            const startedAt = new Date().toISOString();
            let timerInterval = null;

            function updateStatus(stage) {
                statusEl.innerHTML = `<span class="watched-card-refreshing"><span class="watched-refresh-spinner"></span>${escapeHtml(formatRefreshStage(stage, startedAt, 'listenbrainz'))}</span>`;
                statusEl.style.display = 'block';
            }

            updateStatus('fetching');
            timerInterval = setInterval(() => updateStatus('fetching'), 1000);

            try {
                const response = await apiFetch('/api/watched-playlists', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        url: username,
                        refresh_interval_hours: parseFloat(watchedIntervalSelect.value),
                        convert_audio: watchedConvertAudio ? watchedConvertAudio.checked : convertAudioCheckbox.checked,
                        make_m3u: document.getElementById('watchedMakeM3u') ? document.getElementById('watchedMakeM3u').checked : false,
                        use_playlists_dir: document.getElementById('watchedUsePlaylistsDir') ? document.getElementById('watchedUsePlaylistsDir').checked : false,
                        sync_mode: 'mirror',
                        custom_subdir: (document.getElementById('watchedCustomSubdir')?.value || '').trim() || null
                    })
                });

                if (!response.ok) {
                    const ct = response.headers.get('content-type') || '';
                    if (ct.includes('application/json')) {
                        const error = await response.json();
                        throw new Error(error.detail || 'Failed to add ListenBrainz playlists');
                    }
                    const text = (await response.text()).slice(0, 200);
                    throw new Error(`Server error ${response.status}: ${text || response.statusText}`);
                }

                const data = await response.json();
                showToast(`Added ${data.created} ListenBrainz playlist(s)` + (data.skipped ? ` (${data.skipped} already watched)` : ''));
                document.getElementById('lbUsernameInput').value = '';
                toggleLbRow();
                loadWatchedPlaylists();
            } catch (error) {
                watchedError.textContent = error.message;
                watchedError.style.display = 'block';
            } finally {
                clearInterval(timerInterval);
                statusEl.style.display = 'none';
                btn.disabled = false;
                btn.textContent = 'Add';
            }
        }

        async function refreshWatchedPlaylist(playlistId) {
            if (watchedRefreshPending.has(playlistId)) return;
            watchedRefreshPending.set(playlistId, { stage: 'starting', startedAt: new Date().toISOString() });
            loadWatchedPlaylists(false);
            try {
                showToast('Checking for new tracks...');
                const response = await apiFetch(`/api/watched-playlists/${playlistId}/refresh`, {
                    method: 'POST'
                });

                if (!response.ok) throw new Error('Refresh failed');

                const data = await response.json();
                if (data.already_running) {
                    showToast('Refresh already in progress');
                } else if (data.error) {
                    showToast(`Error: ${data.error}`, true);
                } else {
                    const newCount = data.new_tracks || 0;
                    const missingCount = data.missing_tracks || 0;
                    if (newCount > 0 || missingCount > 0) {
                        const parts = [];
                        if (newCount > 0) parts.push(`${newCount} new`);
                        if (missingCount > 0) parts.push(`${missingCount} missing`);
                        showToast(`Found ${parts.join(' + ')} tracks, queued ${data.queued} for download`);
                    } else {
                        showToast('No new tracks found');
                    }
                }
            } catch (error) {
                showToast('Failed to refresh playlist', true);
            } finally {
                watchedRefreshPending.delete(playlistId);
                loadWatchedPlaylists();
            }
        }

        async function copyWatchedPlaylistUrl(url) {
            try {
                if (navigator.clipboard && window.isSecureContext) {
                    await navigator.clipboard.writeText(url);
                } else {
                    const tempInput = document.createElement('input');
                    tempInput.value = url;
                    document.body.appendChild(tempInput);
                    tempInput.select();
                    document.execCommand('copy');
                    document.body.removeChild(tempInput);
                }
                showToast('Playlist URL copied');
            } catch (error) {
                showToast('Failed to copy playlist URL', true);
            }
        }

        async function toggleWatchedPlaylist(playlistId, enabled) {
            const isEnabled = enabled === true || enabled === 'true';
            try {
                const response = await apiFetch(`/api/watched-playlists/${playlistId}`, {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ enabled: isEnabled })
                });

                if (!response.ok) throw new Error('Update failed');

                loadWatchedPlaylists();
                showToast(isEnabled ? 'Playlist watching resumed' : 'Playlist watching paused');
            } catch (error) {
                showToast('Failed to update playlist', true);
            }
        }

        async function updateWatchedPlaylistConversion(playlistId, convertAudio) {
            try {
                const response = await apiFetch(`/api/watched-playlists/${playlistId}`, {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ convert_audio: convertAudio })
                });

                if (!response.ok) throw new Error('Update failed');

                showToast(conversionChoiceLabel(convertAudio));
            } catch (error) {
                showToast('Failed to update format setting', true);
                loadWatchedPlaylists();
            }
        }

        async function updateWatchedPlaylistM3u(playlistId, makeM3u) {
            try {
                const response = await apiFetch(`/api/watched-playlists/${playlistId}`, {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ make_m3u: makeM3u })
                });

                if (!response.ok) throw new Error('Update failed');

                showToast(makeM3u ? 'M3U generation enabled' : 'M3U generation disabled');
            } catch (error) {
                showToast('Failed to update M3U setting', true);
                loadWatchedPlaylists();
            }
        }

        async function updateWatchedPlaylistUsePlaylists(playlistId, usePlaylists) {
            try {
                const response = await apiFetch(`/api/watched-playlists/${playlistId}`, {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ use_playlists_dir: usePlaylists })
                });

                if (!response.ok) throw new Error('Update failed');

                showToast(usePlaylists ? 'Downloads going to Playlists folder' : 'Downloads going to Singles folder');
            } catch (error) {
                showToast('Failed to update Playlists folder setting', true);
                loadWatchedPlaylists();
            }
        }

        async function updateWatchedPlaylistRedownloadMissing(playlistId, value) {
            const setting = value === 'inherit' ? null : value === 'true';
            try {
                const response = await apiFetch(`/api/watched-playlists/${playlistId}`, {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ redownload_missing_files: setting })
                });
                if (!response.ok) throw new Error('Update failed');
                showToast(value === 'inherit'
                    ? 'Missing files now follow the global setting'
                    : setting ? 'Missing files will be re-downloaded' : 'Missing files will be left alone');
                loadWatchedPlaylists(false);
            } catch (error) {
                showToast('Failed to update missing-file setting', true);
                loadWatchedPlaylists();
            }
        }

        async function toggleMissingTracks(playlistId) {
            const panel = document.getElementById(`missing-${playlistId}`);
            if (!panel) return;

            if (panel.style.display !== 'none') {
                panel.style.display = 'none';
                return;
            }

            panel.style.display = 'block';
            panel.textContent = 'Loading...';

            try {
                const resp = await apiFetch(`/api/watched-playlists/${playlistId}/missing`);
                if (!resp.ok) throw new Error('Failed to fetch');
                const data = await resp.json();

                if (!data.count) {
                    panel.textContent = 'No missing tracks - everything downloaded successfully.';
                    return;
                }

                const rows = data.missing.map((t, i) => {
                    const removed = t.removed_at ? ' <span style="color: var(--text-secondary); font-size: 10px;">(removed upstream)</span>' : '';
                    const trackId = `missing-track-${playlistId}-${i}`;
                    return `<div id="${trackId}" style="padding: 4px 0; display: flex; justify-content: space-between; align-items: center; gap: 8px; flex-wrap: wrap;">
                        <span style="flex: 1; min-width: 0;">${escapeHtml(t.artist)} &ndash; ${escapeHtml(t.title)}${removed}</span>
                        <div style="display: flex; gap: 4px; align-items: center; flex-shrink: 0;">
                            <span style="font-size: 10px; color: var(--text-secondary); white-space: nowrap;">${t.job_status || 'not attempted'}</span>
                            <button type="button"
                                data-action="retry-missing-track"
                                data-playlist-id="${escapeAttr(playlistId)}"
                                data-artist="${escapeAttr(t.artist)}"
                                data-title="${escapeAttr(t.title)}"
                                data-row-id="${escapeAttr(trackId)}"
                                style="padding: 3px 8px; font-size: 11px; font-family: inherit; background: var(--bg-tertiary); color: var(--text-secondary); border: 1px solid var(--border); border-radius: 4px; cursor: pointer; white-space: nowrap;"
                                title="Auto-search and re-queue this track">Retry</button>
                            <button type="button"
                                data-action="search-missing-track"
                                data-playlist-id="${escapeAttr(playlistId)}"
                                data-playlist-name="${escapeAttr(data.playlist_name)}"
                                data-artist="${escapeAttr(t.artist)}"
                                data-title="${escapeAttr(t.title)}"
                                style="padding: 3px 8px; font-size: 11px; font-family: inherit; background: var(--bg-tertiary); color: var(--text-secondary); border: 1px solid var(--border); border-radius: 4px; cursor: pointer; white-space: nowrap;"
                                title="Search manually and pick a result">Search</button>
                        </div>
                    </div>`;
                }).join('');

                panel.innerHTML = `<div style="font-weight: 600; margin-bottom: 6px;">${data.count} missing track${data.count !== 1 ? 's' : ''}:</div>${rows}`;
            } catch (e) {
                panel.textContent = 'Could not load missing tracks.';
            }
        }

        async function retryMissingTrack(playlistId, artist, title, rowId) {
            const row = document.getElementById(rowId);
            const retryBtn = row?.querySelector('button');
            if (retryBtn) { retryBtn.disabled = true; retryBtn.textContent = 'Searching...'; }

            try {
                const resp = await apiFetch(`/api/watched-playlists/${playlistId}/retry-track`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ artist, title })
                });
                if (!resp.ok) throw new Error('Failed');
                showToast(`Queued search for ${artist} - ${title}`);
                if (retryBtn) { retryBtn.textContent = 'Queued'; }
            } catch (e) {
                showToast('Failed to retry track', true);
                if (retryBtn) { retryBtn.disabled = false; retryBtn.textContent = 'Retry'; }
            }
        }

        async function searchMissingTrack(playlistId, playlistName, artist, title) {
            // Pre-fill the search input
            setSearchValue(`${artist} - ${title}`);

            // Open the destination picker in playlist mode and pre-select this playlist,
            // so the user can immediately download straight back into the right playlist.
            const sel = document.getElementById('playlistSelectorInput');
            const playlistPanel = document.getElementById('playlistSelector');
            const modePanel = document.getElementById('destinationModePanel');
            const pickerToggle = document.getElementById('destinationPickerToggle');
            if (sel && playlistName) {
                await loadPlaylists();
                let found = false;
                for (const opt of sel.options) {
                    if (opt.value === playlistName) {
                        sel.value = playlistName;
                        found = true;
                        break;
                    }
                }
                _destinationMode = 'playlist';
                if (modePanel) modePanel.style.display = 'none';
                if (playlistPanel) playlistPanel.style.display = 'flex';
                if (pickerToggle) {
                    pickerToggle.textContent = 'Adding to playlist';
                    pickerToggle.classList.add('active');
                }
                if (!found) sel.value = '';
                _updatePlaylistSelectorWarning();
            }

            // Switch to the Tracks tab (data-tab is still "results") and fire the search
            const resultsTabBtn = document.querySelector('.tab[data-tab="results"]');
            if (resultsTabBtn) resultsTabBtn.click();

            search();
        }

        async function toggleTrackList(playlistId, playlistName) {
            const panel = document.getElementById(`tracks-${playlistId}`);
            if (!panel) return;

            if (panel.style.display !== 'none') {
                panel.style.display = 'none';
                return;
            }

            panel.style.display = 'block';
            panel.textContent = 'Loading...';

            try {
                const resp = await apiFetch(`/api/watched-playlists/${playlistId}/tracks`);
                if (!resp.ok) throw new Error('Failed to fetch');
                const data = await resp.json();

                if (!data.total && !data.departed) {
                    panel.textContent = 'No tracks tracked yet.';
                    return;
                }

                const tracks = data.tracks;
                // Sections are mutually exclusive: a departed track belongs in its own
                // group whatever its download state, so the headings add up to the
                // summary line instead of counting the same track twice.
                const sections = {
                    downloaded: tracks.filter(t => t.downloaded_at && !t.removed_at),
                    failed: tracks.filter(t => !t.downloaded_at && ['failed', 'completed_with_errors', null].includes(t.job_status) && !t.removed_at),
                    pending: tracks.filter(t => !t.downloaded_at && ['queued', 'downloading'].includes(t.job_status) && !t.removed_at),
                    removed: tracks.filter(t => t.removed_at),
                };

                const departedSummary = data.departed
                    ? ` &middot; ${data.departed} ${data.sync_mode === 'mirror' ? 'no longer upstream' : 'kept from earlier'}`
                    : '';
                let html = `<div class="track-list-summary">${data.downloaded} downloaded &middot; ${data.failed} failed &middot; ${data.pending} pending${departedSummary}</div>`;

                function trackRow(t, i, actions) {
                    const rowId = `tl-track-${playlistId}-${i}`;
                    return `<div id="${rowId}" class="track-list-row">
                        <span class="track-list-label">${escapeHtml(t.artist)} &ndash; ${escapeHtml(t.title)}</span>
                        <div class="track-list-actions">${actions(t, rowId)}</div>
                    </div>`;
                }

                if (sections.downloaded.length) {
                    html += `<div class="track-list-section-header">Downloaded (${sections.downloaded.length})</div>`;
                    html += sections.downloaded.map((t, i) => trackRow(t, `d${i}`, (t, rowId) =>
                        `<span class="track-status-chip track-status-ok">&#10003;</span>
                         ${t.job_id ? `<button onclick="saveJobToDevice('${escapeAttr(t.job_id)}')" title="Save this track to your device" class="track-action-btn">
                             <i class="fa-solid fa-download"></i></button>` : ''}
                         <button type="button"
                             data-action="replace-watched-track"
                             data-playlist-id="${escapeAttr(playlistId)}"
                             data-playlist-name="${escapeAttr(data.playlist_name)}"
                             data-artist="${escapeAttr(t.artist)}"
                             data-title="${escapeAttr(t.title)}"
                             data-track-hash="${escapeAttr(t.track_hash)}"
                             data-row-id="${escapeAttr(rowId)}"
                             class="track-replace-btn" title="Delete this file and search for the correct version">Replace</button>`
                    )).join('');
                }

                if (sections.failed.length) {
                    html += `<div class="track-list-section-header">Failed / Missing (${sections.failed.length})</div>`;
                    html += sections.failed.map((t, i) => trackRow(t, `f${i}`, (t, rowId) =>
                        `<span class="track-status-chip track-status-fail">&#10007;</span>
                         <button type="button"
                             data-action="retry-missing-track"
                             data-playlist-id="${escapeAttr(playlistId)}"
                             data-playlist-name="${escapeAttr(data.playlist_name)}"
                             data-artist="${escapeAttr(t.artist)}"
                             data-title="${escapeAttr(t.title)}"
                             data-row-id="${escapeAttr(rowId)}"
                             class="track-action-btn" title="Auto-search and re-queue">Retry</button>
                         <button type="button"
                             data-action="find-missing-track-versions"
                             data-playlist-id="${escapeAttr(playlistId)}"
                             data-playlist-name="${escapeAttr(data.playlist_name)}"
                             data-artist="${escapeAttr(t.artist)}"
                             data-title="${escapeAttr(t.title)}"
                             data-row-id="${escapeAttr(rowId)}"
                             class="track-action-btn" title="Preview and pick from the top alternatives">Find Versions</button>
                         <button type="button"
                             data-action="search-missing-track"
                             data-playlist-id="${escapeAttr(playlistId)}"
                             data-playlist-name="${escapeAttr(data.playlist_name)}"
                             data-artist="${escapeAttr(t.artist)}"
                             data-title="${escapeAttr(t.title)}"
                             class="track-action-btn" title="Search manually">Search</button>`
                    )).join('');
                }

                if (sections.pending.length) {
                    html += `<div class="track-list-section-header">In Progress (${sections.pending.length})</div>`;
                    html += sections.pending.map((t, i) => trackRow(t, `p${i}`, () =>
                        `<span class="track-status-chip track-status-pending">&#8987;</span>`
                    )).join('');
                }

                if (sections.removed.length) {
                    const removedHeading = data.sync_mode === 'mirror'
                        ? `Removed Upstream (${sections.removed.length})`
                        : `No Longer Upstream, Kept (${sections.removed.length})`;
                    html += `<div class="track-list-section-header">${removedHeading}</div>`;
                    html += sections.removed.map((t, i) => trackRow(t, `r${i}`, (t) =>
                        `<span class="track-status-chip track-status-removed">${t.downloaded_at ? 'kept' : 'gone'}</span>`
                    )).join('');
                }

                panel.innerHTML = html;
            } catch (e) {
                panel.textContent = 'Could not load tracks.';
            }
        }

        async function replaceTrack(playlistId, playlistName, artist, title, trackHash, rowId) {
            const row = document.getElementById(rowId);
            const btn = row?.querySelector('.track-replace-btn');
            if (btn) { btn.disabled = true; btn.textContent = 'Trashing...'; }

            try {
                // Trash (or unlink from playlist) the bad file and clear downloaded_at
                const resp = await apiFetch(
                    `/api/watched-playlists/${encodeURIComponent(playlistId)}/tracks/${encodeURIComponent(trackHash)}/file`,
                    { method: 'DELETE' }
                );
                if (!resp.ok) {
                    // A generic "Failed to trash file" told nobody anything. Pass on
                    // whatever the backend actually said instead.
                    const body = await resp.json().catch(() => null);
                    throw new Error(body?.detail || `HTTP ${resp.status}`);
                }
                const data = await resp.json();

                showToast(`Trashed ${artist} - ${title}. Pick the correct version below.`);

                // Update the row to show missing state with retry/search options
                if (row) {
                    const actionsEl = row.querySelector('.track-list-actions');
                    if (actionsEl) {
                        actionsEl.innerHTML = `<span class="track-status-chip track-status-fail">missing</span>
                            <button type="button"
                                data-action="retry-missing-track"
                                data-playlist-id="${escapeAttr(playlistId)}"
                                data-playlist-name="${escapeAttr(playlistName)}"
                                data-artist="${escapeAttr(artist)}"
                                data-title="${escapeAttr(title)}"
                                data-row-id="${escapeAttr(rowId)}"
                                class="track-action-btn">Retry</button>
                            <button type="button"
                                data-action="find-missing-track-versions"
                                data-playlist-id="${escapeAttr(playlistId)}"
                                data-playlist-name="${escapeAttr(playlistName)}"
                                data-artist="${escapeAttr(artist)}"
                                data-title="${escapeAttr(title)}"
                                data-row-id="${escapeAttr(rowId)}"
                                class="track-action-btn">Find Versions</button>
                            <button type="button"
                                data-action="search-missing-track"
                                data-playlist-id="${escapeAttr(playlistId)}"
                                data-playlist-name="${escapeAttr(playlistName)}"
                                data-artist="${escapeAttr(artist)}"
                                data-title="${escapeAttr(title)}"
                                class="track-action-btn">Search</button>`;
                    }
                }

                // Route to search tab pre-filled with this track + playlist selected
                searchMissingTrack(playlistId, playlistName, artist, title);
            } catch (e) {
                showToast(`Failed to trash file: ${e.message || e}`, true);
                if (btn) { btn.disabled = false; btn.textContent = 'Replace'; }
            }
        }

        async function updateWatchedPlaylistSyncMode(playlistId, syncMode) {
            try {
                const response = await apiFetch(`/api/watched-playlists/${playlistId}`, {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ sync_mode: syncMode })
                });

                if (!response.ok) throw new Error('Update failed');

                showToast(syncMode === 'mirror' ? 'Sync mode: Mirror (M3U tracks upstream)' : 'Sync mode: Append (M3U grows over time)');
            } catch (error) {
                showToast('Failed to update sync mode', true);
                loadWatchedPlaylists();
            }
        }

        function clearWatchedCustomSubdir() {
            const input = document.getElementById('watchedCustomSubdir');
            const btn = document.getElementById('watchedClearDestBtn');
            const display = document.getElementById('watchedDestDisplay');
            if (input) input.value = '';
            if (btn) btn.style.display = 'none';
            if (display) display.textContent = '';
        }

        function updateWatchedDestDisplay() {
            const val = (document.getElementById('watchedCustomSubdir')?.value || '').trim();
            const display = document.getElementById('watchedDestDisplay');
            const clearBtn = document.getElementById('watchedClearDestBtn');
            const musicRoot = (serverConfig?.music_dir || '/music').replace(/\/+$/, '');
            if (display) display.textContent = val ? `${musicRoot}/${val}` : '';
            if (clearBtn) clearBtn.style.display = val ? 'inline-flex' : 'none';
        }

        // =============================================================================
        // Destination folder picker modal
        // =============================================================================
        let _destPickerCallback = null;
        let _destPickerBrowsePath = '';   // directory currently being listed
        let _destPickerSelected = '';     // path currently selected (confirmed in input)

        async function openDestPicker(currentValue, callback) {
            _destPickerCallback = callback;
            _destPickerSelected = (currentValue || '').trim();
            _destPickerBrowsePath = _destPickerSelected
                ? _destPickerSelected.split('/').slice(0, -1).join('/')
                : '';
            const input = document.getElementById('destPickerCustomInput');
            if (input) input.value = _destPickerSelected;
            const overlay = document.getElementById('destPickerOverlay');
            if (overlay) overlay.style.display = 'flex';
            await _destPickerBrowse(_destPickerBrowsePath);
        }

        function closeDestPicker() {
            const overlay = document.getElementById('destPickerOverlay');
            if (overlay) overlay.style.display = 'none';
            _destPickerCallback = null;
        }

        function confirmDestPicker() {
            const raw = (document.getElementById('destPickerCustomInput')?.value || '').trim();
            if (_destPickerCallback) _destPickerCallback(raw);
            closeDestPicker();
        }

        function destPickerOnCustomInput(value) {
            _destPickerSelected = value.trim();
            _destPickerRenderBreadcrumb(_destPickerBrowsePath);
        }

        async function _destPickerBrowse(path) {
            _destPickerBrowsePath = path;
            _destPickerRenderBreadcrumb(path);
            const listEl = document.getElementById('destPickerList');
            if (!listEl) return;
            listEl.innerHTML = '<div class="dest-picker-loading">Loading...</div>';
            try {
                const encoded = path ? encodeURIComponent(path) : '';
                const resp = await apiFetch(`/api/music-dirs${encoded ? '?path=' + encoded : ''}`);
                if (!resp.ok) throw new Error('Failed to load directories');
                const data = await resp.json();
                const dirs = Array.isArray(data.directories) ? data.directories : [];
                listEl.innerHTML = '';
                if (path) {
                    const upBtn = document.createElement('button');
                    upBtn.className = 'dest-picker-item dest-picker-up';
                    upBtn.innerHTML = '<i class="fa-solid fa-arrow-left"></i> Up';
                    upBtn.onclick = () => {
                        const parent = path.includes('/') ? path.split('/').slice(0, -1).join('/') : '';
                        _destPickerBrowse(parent);
                    };
                    listEl.appendChild(upBtn);
                    // "Select this folder" button for the current browsed path
                    const selfBtn = document.createElement('button');
                    selfBtn.className = 'dest-picker-item dest-picker-self';
                    selfBtn.innerHTML = `<i class="fa-solid fa-check"></i> Use: <strong>${escapeHtml(path)}</strong>`;
                    selfBtn.onclick = () => _destPickerSetValue(path);
                    listEl.appendChild(selfBtn);
                } else {
                    const rootBtn = document.createElement('button');
                    rootBtn.className = 'dest-picker-item dest-picker-self';
                    rootBtn.innerHTML = '<i class="fa-solid fa-check"></i> Use: <strong>music root</strong>';
                    rootBtn.onclick = () => _destPickerSetValue('.');
                    listEl.appendChild(rootBtn);
                }
                if (dirs.length === 0) {
                    const empty = document.createElement('div');
                    empty.className = 'dest-picker-empty';
                    empty.textContent = 'No subdirectories here.';
                    listEl.appendChild(empty);
                }
                for (const dir of dirs) {
                    const btn = document.createElement('button');
                    btn.className = 'dest-picker-item';
                    const name = dir.split('/').pop();
                    btn.innerHTML = `<i class="fa-solid fa-folder"></i> ${escapeHtml(name)}`;
                    btn.onclick = () => _destPickerBrowse(dir);
                    listEl.appendChild(btn);
                }
            } catch (e) {
                listEl.innerHTML = '<div class="dest-picker-empty">Could not load directories.</div>';
            }
        }

        function _destPickerSetValue(path) {
            _destPickerSelected = path;
            const input = document.getElementById('destPickerCustomInput');
            if (input) input.value = path === '.' ? '.' : path;
        }

        function _destPickerRenderBreadcrumb(path) {
            const el = document.getElementById('destPickerBreadcrumb');
            if (!el) return;
            const musicRoot = (serverConfig?.music_dir || '/music').replace(/\/+$/, '');
            const parts = path ? path.split('/') : [];
            let html = `<span class="dest-crumb dest-crumb-root" onclick="_destPickerBrowse('')">${escapeHtml(musicRoot)}</span>`;
            let cumulative = '';
            for (const part of parts) {
                cumulative = cumulative ? `${cumulative}/${part}` : part;
                const captured = cumulative;
                html += ` <span class="dest-crumb-sep">/</span> <span class="dest-crumb" onclick="_destPickerBrowse('${escapeAttr(captured)}')">${escapeHtml(part)}</span>`;
            }
            el.innerHTML = html;
        }

        async function updateWatchedPlaylistCustomSubdir(playlistId, value) {
            try {
                const response = await apiFetch(`/api/watched-playlists/${playlistId}`, {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ custom_subdir: value || null })
                });
                if (!response.ok) throw new Error('Update failed');
                showToast(value ? `Destination: ${value}` : 'Destination reset to default');
                loadWatchedPlaylists();
            } catch (error) {
                showToast('Failed to update destination', true);
                loadWatchedPlaylists();
            }
        }

        // Source chips: render per-source toggle chips for a watched playlist card or the add form
        let _cachedSources = null;
        async function _fetchSources() {
            if (_cachedSources) return _cachedSources;
            try {
                const res = await apiFetch('/api/sources');
                if (res.ok) _cachedSources = (await res.json()).sources || [];
            } catch {}
            return _cachedSources || [];
        }

        function renderSourceChips(playlistId, preferredSources) {
            // Only show globally-enabled sources; repopulate once sources load if cache is empty
            const sources = (_cachedSources || []).filter(s => s.enabled);
            const active = preferredSources === 'all' ? [] : preferredSources.split(',').map(s => s.trim());
            return sources.map(s => {
                const on = active.length === 0 || active.includes(s.id);
                return `<button type="button" class="source-chip ${on ? 'on' : 'off'}" data-source="${escapeAttr(s.id)}"
                    onclick="toggleSourceChip(this, '${escapeAttr(playlistId)}')"
                    title="${escapeAttr(s.label)}">${escapeHtml(s.badge)}</button>`;
            }).join('');
        }

        async function populateSourceChips() {
            await _fetchSources();
            // Re-render any chips containers that used stale/empty data
            document.querySelectorAll('.watched-sources-chips[data-playlist-id]').forEach(el => {
                const pid = el.dataset.playlistId;
                // Use the authoritative preferred_sources from the API, not the DOM chip state
                const pref = el.dataset.preferred || 'all';
                el.innerHTML = renderSourceChips(pid, pref);
            });
            // Same dance for per-playlist priority-source dropdowns: they may have
            // been rendered before _cachedSources populated, so the only option was
            // "No preference". Re-fill them now using the stored value as the default.
            document.querySelectorAll('select[data-priority-source]').forEach(sel => {
                const current = sel.dataset.prioritySource || '';
                sel.innerHTML = renderPrioritySourceOptions(current);
            });
            // Populate the add-form selector (only globally-enabled sources, all on by default)
            const formSel = document.getElementById('watchedSourcesSelector');
            if (formSel && formSel.children.length === 0) {
                const sources = (_cachedSources || []).filter(s => s.enabled);
                formSel.innerHTML = sources.map(s =>
                    `<button type="button" class="source-chip on" data-source="${escapeAttr(s.id)}"
                        onclick="this.classList.toggle('on'); this.classList.toggle('off')"
                        title="${escapeAttr(s.label)}">${escapeHtml(s.badge)}</button>`
                ).join('');
            }
            // Populate priority-source dropdowns (one in bulk import, one in watched add form)
            populatePrioritySourceDropdowns();
        }

        function populatePrioritySourceDropdowns() {
            const sources = (_cachedSources || []).filter(s => s.enabled);
            const targets = ['bulkPrioritySource', 'watchedPrioritySource'];
            for (const id of targets) {
                const sel = document.getElementById(id);
                if (!sel || sel.options.length > 1) continue;  // already populated
                for (const s of sources) {
                    const opt = document.createElement('option');
                    opt.value = s.id;
                    opt.textContent = s.label;
                    sel.appendChild(opt);
                }
            }
        }

        function toggleSourceChip(btn, playlistId) {
            btn.classList.toggle('on');
            btn.classList.toggle('off');
            // Collect current state for this playlist
            const container = btn.closest('.watched-sources-chips');
            const chips = [...container.querySelectorAll('.source-chip')];
            const on = chips.filter(c => c.classList.contains('on')).map(c => c.dataset.source);
            // "all on" = send "all"; partial = comma list; none = "all" (fallback, don't allow locking out)
            const preferred = (on.length === 0 || on.length === chips.length) ? 'all' : on.join(',');
            container.dataset.preferred = preferred;
            updateWatchedPlaylistPreferredSources(playlistId, preferred);
        }

        function getWatchedPreferredSources() {
            const chips = [...document.querySelectorAll('#watchedSourcesSelector .source-chip')];
            if (chips.length === 0) return 'all';
            const on = chips.filter(c => c.classList.contains('on')).map(c => c.dataset.source);
            return (on.length === 0 || on.length === chips.length) ? 'all' : on.join(',');
        }

        function setWatchedPreferredSource(sourceId) {
            // Flip the add-form source chips to a single source, leaving others off
            const chips = [...document.querySelectorAll('#watchedSourcesSelector .source-chip')];
            chips.forEach(c => {
                const match = c.dataset.source === sourceId;
                c.classList.toggle('on', match);
                c.classList.toggle('off', !match);
            });
        }

        async function updateWatchedPlaylistPreferredSources(playlistId, preferred) {
            try {
                const response = await apiFetch(`/api/watched-playlists/${playlistId}`, {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ preferred_sources: preferred })
                });
                if (!response.ok) throw new Error('Update failed');
                const label = preferred === 'all' ? 'all sources' : preferred;
                showToast(`Sources: ${label}`);
            } catch (error) {
                showToast('Failed to update sources', true);
                loadWatchedPlaylists();
            }
        }

        function renderPrioritySourceOptions(current) {
            const sources = (_cachedSources || []).filter(s => s.enabled);
            const cur = (current || '').toLowerCase();
            const opts = [`<option value="" ${cur ? '' : 'selected'}>No preference</option>`];
            for (const s of sources) {
                const sel = cur === s.id ? 'selected' : '';
                opts.push(`<option value="${escapeAttr(s.id)}" ${sel}>${escapeHtml(s.label)}</option>`);
            }
            return opts.join('');
        }

        async function updateWatchedPlaylistPrioritySource(playlistId, priority) {
            try {
                const response = await apiFetch(`/api/watched-playlists/${playlistId}`, {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ priority_source: priority || '' })
                });
                if (!response.ok) throw new Error('Update failed');
                showToast(priority ? `Preferred source: ${priority}` : 'Preferred source cleared');
            } catch (error) {
                showToast('Failed to update preferred source', true);
                loadWatchedPlaylists();
            }
        }

        async function deleteWatchedPlaylist(playlistId, name) {
            if (!confirm(`Stop watching "${name}"? This won't delete any downloaded files.`)) {
                return;
            }

            try {
                const response = await apiFetch(`/api/watched-playlists/${playlistId}`, {
                    method: 'DELETE'
                });

                if (!response.ok) throw new Error('Delete failed');

                showToast(`Stopped watching "${name}"`);
                loadWatchedPlaylists();
            } catch (error) {
                showToast('Failed to delete playlist', true);
            }
        }

        async function refreshAllWatched() {
            refreshAllWatchedBtn.disabled = true;
            refreshAllWatchedBtn.textContent = 'Checking...';

            try {
                const response = await apiFetch('/api/watched-playlists/check-all', {
                    method: 'POST'
                });

                if (!response.ok) throw new Error('Check failed');

                const data = await response.json();
                if (data.checked === 0) {
                    showToast('No playlists due for refresh');
                } else if (data.total_new_tracks > 0) {
                    showToast(`Checked ${data.checked} playlists: ${data.total_new_tracks} new tracks, ${data.total_queued} queued`);
                } else {
                    showToast(`Checked ${data.checked} playlists: no new tracks`);
                }
                loadWatchedPlaylists();
            } catch (error) {
                showToast('Failed to check playlists', true);
            } finally {
                refreshAllWatchedBtn.disabled = false;
                refreshAllWatchedBtn.textContent = 'Check All Now';
            }
        }

        // Event listeners for watched playlists
        addWatchedBtn.addEventListener('click', addWatchedPlaylist);
        watchedUrlInput.addEventListener('keypress', (e) => {
            if (e.key === 'Enter') addWatchedPlaylist();
        });
        document.getElementById('lbUsernameInput').addEventListener('keypress', (e) => {
            if (e.key === 'Enter') addListenBrainzPlaylists();
        });
        refreshAllWatchedBtn.addEventListener('click', refreshAllWatched);

        // Watched Upgrades controls (pager + rescan)
        (function wireUpgradeControls() {
            const prev = document.getElementById('upgradesPrevBtn');
            const next = document.getElementById('upgradesNextBtn');
            const rescan = document.getElementById('rescanUpgradesBtn');
            if (prev) prev.addEventListener('click', () => { if (upgradesPage > 1) { upgradesPage--; loadWatchedUpgrades(); } });
            if (next) next.addEventListener('click', () => { upgradesPage++; loadWatchedUpgrades(); });
            if (rescan) rescan.addEventListener('click', async () => {
                rescan.disabled = true;
                const original = rescan.textContent;
                rescan.textContent = 'Scanning...';
                try {
                    const resp = await apiFetch('/api/upgrades/rescan', { method: 'POST' });
                    if (resp.ok) {
                        const data = await resp.json();
                        const t = data.totals || {};
                        showToast(`Scan done: ${t.below_target || 0} below target across ${t.eligible || 0} files`);
                        upgradesPage = 1;
                        loadWatchedUpgrades();
                    } else if (resp.status === 400) {
                        showToast('Enable Track Upgrades in Settings first', true);
                    } else if (resp.status === 403) {
                        showToast('Admin only', true);
                    }
                } catch (e) {
                    showToast('Scan failed', true);
                } finally {
                    rescan.disabled = false;
                    rescan.textContent = original;
                }
            });
        })();

        // Quarantine panel: files a Track Upgrade swap displaced. Collapsed by
        // default; loaded on every open so it never shows stale state.
        (function wireQuarantineControls() {
            const toggle = document.getElementById('quarantineToggleBtn');
            const panel = document.getElementById('quarantinePanel');
            if (!toggle || !panel) return;
            toggle.addEventListener('click', () => {
                const opening = panel.style.display === 'none';
                panel.style.display = opening ? '' : 'none';
                if (opening) loadQuarantine();
            });
        })();

        // =============================================================================
        // Watched Artists
        // =============================================================================

        let selectedArtistMbid = null;
        let selectedArtistName = null;
        let artistRefreshPending = new Map(); // artist_id -> {stage, startedAt}
        let artistRefreshPollInterval = null;
        let watchedArtistsById = new Map(); // artist_id -> full row, so the Albums panel can look up mbid/auto_add_albums

        function startArtistRefreshPolling() {
            if (artistRefreshPollInterval) return;
            artistRefreshPollInterval = setInterval(() => loadWatchedArtists(false), 2000);
        }

        function stopArtistRefreshPolling() {
            if (artistRefreshPollInterval) {
                clearInterval(artistRefreshPollInterval);
                artistRefreshPollInterval = null;
            }
        }

        async function searchArtist() {
            const q = document.getElementById('artistSearchInput').value.trim();
            if (!q) return;
            const resultsEl = document.getElementById('artistSearchResults');
            const addForm = document.getElementById('artistAddForm');
            resultsEl.style.display = 'block';
            resultsEl.innerHTML = '<div style="font-size:12px;color:var(--text-secondary);">Searching MusicBrainz...</div>';
            addForm.style.display = 'none';
            selectedArtistMbid = null;
            selectedArtistName = null;
            try {
                const res = await apiFetch(`/api/watched-artists/search?q=${encodeURIComponent(q)}`);
                // A 503 means MusicBrainz is down, not that the artist does not exist.
                // Without this it parsed happily, found no results, and said so.
                if (!res.ok) {
                    let detail = `HTTP ${res.status}`;
                    try { detail = (await res.json()).detail || detail; } catch (_) { /* not JSON, keep the status */ }
                    resultsEl.innerHTML = `<div style="font-size:12px;color:var(--error);">Search failed: ${escapeHtml(detail)}</div>`;
                    return;
                }
                const data = await res.json();
                if (!data.results || data.results.length === 0) {
                    resultsEl.innerHTML = '<div style="font-size:12px;color:var(--text-secondary);">No artists found on MusicBrainz.</div>';
                    return;
                }
                // Collapse to a single candidate only when exactly one artist owns
                // the name. Plenty of names are shared: four different acts answer
                // to "Raye", and quietly picking whichever MusicBrainz listed first
                // hands you an anonymous feature credit instead of the singer you
                // meant. When the name is contested, show the field and let the
                // disambiguation text do its job.
                const qLower = q.toLowerCase();
                const exactMatches = data.results.filter(a => (a.name || '').toLowerCase() === qLower);
                const candidates = exactMatches.length === 1
                    ? exactMatches
                    : data.results.slice(0, 5);
                resultsEl.innerHTML = candidates.map(a => `
                    <div style="display:flex;align-items:center;gap:10px;padding:6px 10px;margin-bottom:4px;background:var(--bg-tertiary);border:1px solid var(--border);border-radius:8px;">
                        <div style="flex:1;">
                            <span style="font-size:13px;font-weight:600;color:var(--text-primary);">${escapeHtml(a.name)}</span>
                            ${a.disambiguation ? `<span style="font-size:11px;color:var(--text-secondary);margin-left:6px;">${escapeHtml(a.disambiguation)}</span>` : ''}
                        </div>
                        <button class="action-btn" style="padding:4px 10px;font-size:12px;" onclick="selectArtist('${escapeAttr(a.mbid)}','${escapeAttr(a.name)}')">Select</button>
                    </div>
                `).join('');
            } catch (e) {
                resultsEl.innerHTML = '<div style="font-size:12px;color:var(--error);">Search failed. Check server logs.</div>';
            }
        }

        function selectArtist(mbid, name) {
            selectedArtistMbid = mbid;
            selectedArtistName = name;
            document.getElementById('selectedArtistName').textContent = name;
            // Default from_date to today in YYYY-MM-DD
            const today = new Date().toISOString().slice(0, 10);
            document.getElementById('artistFromDate').value = today;
            document.getElementById('artistSearchResults').style.display = 'none';

            // Picking the artist no longer commits you to anything. Ask what they
            // are actually here for: singles, albums, or one album and out.
            hideArtistModePanels();
            const chooserName = document.getElementById('artistChooserName');
            if (chooserName) chooserName.textContent = name;
            const chooser = document.getElementById('artistModeChooser');
            if (chooser) chooser.style.display = 'block';
        }

        // Everything the chooser can reveal, so switching mode never leaves two
        // panels arguing over the same bit of screen.
        function hideArtistModePanels() {
            for (const id of ['artistModeChooser', 'artistAddForm',
                              'artistAlbumsFollowPanel', 'artistOneOffPanel']) {
                const el = document.getElementById(id);
                if (el) el.style.display = 'none';
            }
            const status = document.getElementById('artistAddStatus');
            if (status) status.style.display = 'none';
        }

        // Back to a blank slate: no artist selected, no panel open, nothing half-done.
        function resetArtistChooser() {
            hideArtistModePanels();
            selectedArtistMbid = null;
            selectedArtistName = null;
            const results = document.getElementById('artistSearchResults');
            if (results) { results.style.display = 'none'; results.innerHTML = ''; }
            const input = document.getElementById('artistSearchInput');
            if (input) input.value = '';
        }

        function chooseArtistMode(mode) {
            if (!selectedArtistMbid) return;
            hideArtistModePanels();
            if (mode === 'singles') {
                const panel = document.getElementById('artistAddForm');
                if (panel) panel.style.display = 'block';
                const addBtn = document.getElementById('addArtistBtn');
                if (addBtn) addBtn.disabled = false;
            } else if (mode === 'albums') {
                const panel = document.getElementById('artistAlbumsFollowPanel');
                if (panel) panel.style.display = 'block';
                const nameEl = document.getElementById('artistAlbumsFollowName');
                if (nameEl) nameEl.textContent = selectedArtistName;
                const toggle = document.getElementById('artistFollowAlbumsToggle');
                if (toggle) toggle.checked = false;
                const progress = document.getElementById('artistAlbumBatchProgress');
                if (progress) { progress.style.display = 'none'; progress.textContent = ''; }
                const batchBtn = document.getElementById('artistAlbumBatchBtn');
                if (batchBtn) batchBtn.disabled = false;
                renderAlbumChecklist(selectedArtistMbid, selectedArtistName);
            } else if (mode === 'oneoff') {
                const handle = showOneOffAlbumSection(selectedArtistName);
                if (handle) handle.setArtist(selectedArtistMbid, selectedArtistName);
            }
        }

        // =====================================================================
        // Follow for Albums: the whole discography, tickable
        // =====================================================================

        let artistChecklistAlbums = [];

        function updateAlbumChecklistCount() {
            const countEl = document.getElementById('artistAlbumChecklistCount');
            if (!countEl) return;
            const ticked = document.querySelectorAll('#artistAlbumChecklist input[type="checkbox"]:checked').length;
            const total = artistChecklistAlbums.length;
            countEl.textContent = ticked
                ? `${ticked} of ${total} selected`
                : `${total} album${total === 1 ? '' : 's'}`;
        }

        async function renderAlbumChecklist(mbid, name) {
            const listEl = document.getElementById('artistAlbumChecklist');
            if (!listEl) return;
            artistChecklistAlbums = [];
            listEl.innerHTML = '<p class="bulk-intro-text">Loading albums from MusicBrainz…</p>';
            updateAlbumChecklistCount();
            try {
                const res = await apiFetch(
                    `/api/albums/artist/${encodeURIComponent(mbid)}/albums?artist_name=${encodeURIComponent(name || '')}`
                );
                if (!res.ok) {
                    let detail = `HTTP ${res.status}`;
                    try { detail = (await res.json()).detail || detail; } catch (_) { /* not JSON, keep the status */ }
                    renderAlbumChecklistError(listEl, detail, mbid, name);
                    return;
                }
                const data = await res.json();
                artistChecklistAlbums = data.albums || [];
                if (!artistChecklistAlbums.length) {
                    listEl.innerHTML = '<p class="bulk-intro-text">No studio albums found for this artist.</p>';
                    updateAlbumChecklistCount();
                    return;
                }
                listEl.innerHTML = '';
                artistChecklistAlbums.forEach((album, idx) => {
                    const row = document.createElement('div');
                    row.className = 'album-checklist-row' + (album.on_disk ? ' on-disk' : '');
                    const cb = document.createElement('input');
                    cb.type = 'checkbox';
                    cb.dataset.albumIndex = String(idx);
                    cb.addEventListener('change', updateAlbumChecklistCount);
                    const title = document.createElement('span');
                    title.className = 'album-checklist-title';
                    title.textContent = album.title || '(untitled)';
                    // Clicking the title ticks the box; the tracklist preview still
                    // lives in the one-off browser for anyone who wants the detail.
                    title.addEventListener('click', () => { cb.checked = !cb.checked; updateAlbumChecklistCount(); });
                    row.appendChild(cb);
                    row.appendChild(title);
                    if (album.year) {
                        const year = document.createElement('span');
                        year.className = 'album-checklist-year';
                        year.textContent = album.year;
                        row.appendChild(year);
                    }
                    if (album.on_disk) {
                        const have = document.createElement('span');
                        have.className = 'album-checklist-have';
                        have.textContent = 'already have it';
                        row.appendChild(have);
                    }
                    listEl.appendChild(row);
                });
                updateAlbumChecklistCount();
            } catch (e) {
                renderAlbumChecklistError(listEl, e.message, mbid, name);
            }
        }

        // MusicBrainz has its off days; offer a retry rather than a dead end.
        function renderAlbumChecklistError(listEl, detail, mbid, name) {
            listEl.innerHTML = '';
            const msg = document.createElement('p');
            msg.className = 'bulk-intro-text error-text';
            msg.textContent = `Failed to load albums: ${detail}`;
            const retry = document.createElement('button');
            retry.className = 'btn btn-ghost';
            retry.type = 'button';
            retry.textContent = 'Retry';
            retry.addEventListener('click', () => renderAlbumChecklist(mbid, name));
            listEl.appendChild(msg);
            listEl.appendChild(retry);
            updateAlbumChecklistCount();
        }

        async function followArtistForAlbums() {
            if (!selectedArtistMbid) return;
            const followFuture = document.getElementById('artistFollowAlbumsToggle')?.checked;
            const progressEl = document.getElementById('artistAlbumBatchProgress');
            const batchBtn = document.getElementById('artistAlbumBatchBtn');
            // Same checkbox every other album download reads, so albums all behave
            // the same way regardless of which door you came in through.
            const convertAudio = document.getElementById('convertAudio')?.checked ?? false;

            const picked = Array.from(
                document.querySelectorAll('#artistAlbumChecklist input[type="checkbox"]:checked')
            ).map(cb => artistChecklistAlbums[parseInt(cb.dataset.albumIndex, 10)]).filter(Boolean);

            if (!picked.length && !followFuture) {
                showToast('Tick some albums, or switch on future albums. Otherwise there is nothing to do.', true);
                return;
            }

            if (batchBtn) batchBtn.disabled = true;
            if (progressEl) { progressEl.style.display = 'block'; progressEl.textContent = 'Working…'; }

            // Only create a follow when future albums were actually asked for.
            // Ticking a few albums with the toggle off is just shopping, and it
            // would be rude to sign them up to a watch list for it.
            let watchArtistId = null;
            if (followFuture) {
                try {
                    const res = await apiFetch('/api/watched-artists', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({
                            mbid: selectedArtistMbid,
                            name: selectedArtistName,
                            from_date: new Date().toISOString().slice(0, 10),
                            refresh_interval_hours: 24,
                            convert_audio: convertAudio,
                            auto_add_albums: true,
                            watch_singles: false,
                        })
                    });
                    const data = await res.json();
                    if (!res.ok) throw new Error(data.detail || 'Failed to follow artist');
                    watchArtistId = data.id;
                    if (data.warning) showToast(data.warning, true);
                } catch (e) {
                    if (progressEl) progressEl.textContent = `Could not follow artist: ${e.message}`;
                    if (batchBtn) batchBtn.disabled = false;
                    return;
                }
            }

            // Queue the ticked albums one at a time. MusicBrainz allows a request a
            // second, so firing them all at once just gets us politely throttled.
            let queued = 0;
            const failures = [];
            for (let i = 0; i < picked.length; i++) {
                const album = picked[i];
                if (progressEl) progressEl.textContent = `Queueing ${i + 1} of ${picked.length}: ${album.title}…`;
                try {
                    let releaseMbid = album.release_mbid;
                    if (!releaseMbid && album.release_group_mbid) {
                        // The list only carries a release-group id now; resolve the
                        // actual pressing here, one album at a time, same pacing
                        // this loop already uses for MusicBrainz.
                        const resolveRes = await apiFetch('/api/albums/resolve-release-group', {
                            method: 'POST',
                            headers: {'Content-Type': 'application/json'},
                            body: JSON.stringify({ release_group_mbid: album.release_group_mbid }),
                        });
                        const resolveData = await resolveRes.json().catch(() => ({}));
                        if (!resolveRes.ok) throw new Error(resolveData.detail || 'Could not resolve a release for this album');
                        releaseMbid = resolveData.release_mbid;
                    }
                    const res = await apiFetch('/api/albums/download', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({
                            artist: selectedArtistName,
                            album_title: album.title,
                            release_mbid: releaseMbid,
                            release_group_mbid: album.release_group_mbid || '',
                            convert_audio: convertAudio,
                            watch_artist_id: watchArtistId || undefined,
                        })
                    });
                    const data = await res.json();
                    if (!res.ok) throw new Error(data.detail || 'Queue failed');
                    queued++;
                } catch (e) {
                    // One duff release should not abandon the other six.
                    failures.push(`${album.title}: ${e.message}`);
                }
            }

            let summary = picked.length
                ? `Queued ${queued} of ${picked.length} album${picked.length === 1 ? '' : 's'}.`
                : '';
            if (followFuture) {
                summary += `${summary ? ' ' : ''}Now following ${selectedArtistName} for new albums.`;
            }
            if (failures.length) summary += ` ${failures.length} failed: ${failures.join('; ')}`;
            if (progressEl) progressEl.textContent = summary;
            showToast(summary, failures.length > 0);

            if (followFuture) {
                loadWatchedArtists();
                resetArtistChooser();
            } else if (batchBtn) {
                batchBtn.disabled = false;
            }
        }

        async function addWatchedArtist() {
            if (!selectedArtistMbid) return;
            const fromDate = document.getElementById('artistFromDate').value;
            const intervalHours = parseInt(document.getElementById('artistIntervalSelect').value);
            const convertAudio = document.getElementById('artistConvertAudio').checked;
            const statusEl = document.getElementById('artistAddStatus');
            const addBtn = document.getElementById('addArtistBtn');

            addBtn.disabled = true;
            statusEl.style.display = 'block';
            statusEl.textContent = 'Adding artist and fetching singles from MusicBrainz...';

            try {
                const res = await apiFetch('/api/watched-artists', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({
                        mbid: selectedArtistMbid,
                        name: selectedArtistName,
                        from_date: fromDate,
                        refresh_interval_hours: intervalHours,
                        convert_audio: convertAudio,
                        watch_singles: true,
                    })
                });
                const data = await res.json();
                if (!res.ok) throw new Error(data.detail || 'Failed to add artist');
                statusEl.style.display = 'none';
                document.getElementById('artistAddForm').style.display = 'none';
                document.getElementById('artistSearchInput').value = '';
                selectedArtistMbid = null;
                selectedArtistName = null;
                // Seeding now runs in the background. Mark it as refreshing so the
                // card shows progress and polling kicks in (avoids a race with the
                // background thread setting refresh_state).
                if (data.id) {
                    artistRefreshPending.set(data.id, { stage: 'starting', startedAt: new Date().toISOString() });
                }
                showToast(`Now watching ${data.name}. Fetching the back-catalogue from MusicBrainz...`);
                loadWatchedArtists();
            } catch (e) {
                statusEl.textContent = `Error: ${e.message}`;
                addBtn.disabled = false;
            }
        }

        // =====================================================================
        // Watched Upgrades (track-upgrades Phase 2a: search + display, no swap yet)
        // =====================================================================
        const UPGRADES_PER_PAGE = 10;
        let upgradesPage = 1;
        let upgradesSearchToken = 0;  // bumped to abort an in-flight lazy-search sweep
        const UPGRADE_SOURCE_BADGES = {
            youtube: 'YT', monochrome: 'MONO', soulseek: 'SLK',
            soundcloud: 'SC', mp3phoenix: 'PX', zvu4no: 'ZV', freemp3cloud: 'FMC',
        };

        function upgradeTierLabel(tier) {
            return { 5: 'Lossless', 4: '320', 3: '256', 2: '192', 1: '128' }[tier] || '?';
        }

        function upgradeCurrentQuality(item) {
            const codec = (item.codec || '?').toUpperCase();
            const storedRate = item.bitrate_kbps ? `${item.bitrate_kbps}kbps` : '';
            const stored = [codec, storedRate].filter(Boolean).join(' ');
            const sourceCodec = (item.source_codec || '').toUpperCase();
            const sourceRate = item.source_bitrate_kbps ? `${item.source_bitrate_kbps}kbps` : '';

            if (item.file_tier === 5) {
                return `${stored} stored · lossless source`;
            }

            // A lossless container can preserve a lossy input perfectly, but it cannot
            // restore quality the input had already discarded. Show both sides so a
            // FLAC-from-MP3 candidate does not look like FLAC is being compared by bitrate.
            if (sourceCodec && sourceCodec !== codec) {
                const source = [sourceCodec, sourceRate].filter(Boolean).join(' ');
                return `${stored} stored · source ${source} · effective ${upgradeTierLabel(item.file_tier)} tier`;
            }

            const legacyOrigin = (item.source_quality || '').match(/\bfrom\s+([^)]*)/i);
            if (legacyOrigin) {
                return `${stored} stored · source ${legacyOrigin[1].trim()} · effective ${upgradeTierLabel(item.file_tier)} tier`;
            }

            return `${stored} · effective ${upgradeTierLabel(item.file_tier)} tier`;
        }

        async function loadWatchedUpgrades() {
            const list = document.getElementById('watchedUpgradesList');
            const pager = document.getElementById('upgradesPager');
            const disabledNote = document.getElementById('upgradesDisabledNote');
            if (!list) return;
            upgradesSearchToken++;  // abort any previous sweep
            try {
                const resp = await apiFetch(`/api/upgrades/candidates?page=${upgradesPage}&per_page=${UPGRADES_PER_PAGE}`);
                if (!resp.ok) { list.innerHTML = ''; return; }
                const data = await resp.json();
                if (data.enabled === false) {
                    if (disabledNote) disabledNote.style.display = '';
                    list.innerHTML = '';
                    if (pager) pager.style.display = 'none';
                    return;
                }
                if (disabledNote) disabledNote.style.display = 'none';

                if (!data.items.length) {
                    list.innerHTML = '<p class="watched-empty-text">No upgrade candidates. Either everything is already at target quality, or the library has not been scanned yet (try Rescan Library).</p>';
                    if (pager) pager.style.display = 'none';
                    return;
                }

                list.innerHTML = data.items.map(renderUpgradeRow).join('');
                data.items.forEach(item => {
                    if (item.found_searched) {
                        renderProposedUpgrade(item);
                    }
                });

                // Pager
                if (pager) {
                    const info = document.getElementById('upgradesPageInfo');
                    if (info) info.textContent = `Page ${data.page} of ${data.pages} (${data.total} below target)`;
                    document.getElementById('upgradesPrevBtn').disabled = data.page <= 1;
                    document.getElementById('upgradesNextBtn').disabled = data.page >= data.pages;
                    pager.style.display = data.pages > 1 ? '' : 'none';
                }

                // Lazily search the rows that have no fresh cached result, ~1/s, this page only.
                lazySearchUpgrades(data.items, ++upgradesSearchToken);
            } catch (e) {
                list.innerHTML = '';
            }
        }

        function renderUpgradeRow(item) {
            const title = escapeHtml(item.title || item.filename || '');
            const artist = escapeHtml(item.artist || '');
            const current = escapeHtml(upgradeCurrentQuality(item));
            return `
                <div class="result-item upgrade-row" id="upgrade-row-${item.id}" data-id="${item.id}">
                    <div class="upgrade-current">
                        <div class="upgrade-track"><strong>${artist}</strong>${artist ? ' &ndash; ' : ''}${title}</div>
                        <div class="upgrade-meta">Current: <span class="upgrade-quality-now">${current}</span></div>
                    </div>
                    <div class="upgrade-proposed" id="upgrade-proposed-${item.id}">
                        <span class="upgrade-searching">Checking for a better copy&hellip;</span>
                    </div>
                    <div class="upgrade-actions">
                        <button class="btn btn-ghost btn-sm" onclick="dismissUpgrade(${item.id})" title="Stop suggesting this one">Dismiss</button>
                    </div>
                </div>`;
        }

        function renderProposedUpgrade(item) {
            const cell = document.getElementById(`upgrade-proposed-${item.id}`);
            if (!cell) return;
            // Stash the source_url on the row so the preview handler can reach it
            // (url-based sources need it; it isn't safe to inline into an attribute).
            const rowEl = document.getElementById(`upgrade-row-${item.id}`);
            if (rowEl) rowEl._foundSourceUrl = item.found_source_url || null;
            if (!item.found_source) {
                cell.innerHTML = '<span class="upgrade-none">No better copy found</span>';
                // Nothing to offer; the server won't list it again once this search is
                // cached, so drop the row now instead of leaving a permanent dead end.
                setTimeout(() => {
                    if (rowEl) rowEl.remove();
                    if (!document.querySelectorAll('#watchedUpgradesList .upgrade-row').length) loadWatchedUpgrades();
                }, 1500);
                return;
            }
            const badge = UPGRADE_SOURCE_BADGES[item.found_source] || item.found_source.toUpperCase().slice(0, 4);
            const tier = upgradeTierLabel(item.found_tier);
            const qual = escapeHtml(item.found_quality || tier);
            const output = escapeHtml(item.upgrade_output_label || 'Keep source format');
            const sourceDescription = `${qual} source`;
            const outputDescription = item.upgrade_conversion_enabled
                ? `${output} output`
                : 'kept in source format';
            const outputTitle = item.upgrade_conversion_enabled
                ? `MusicGrabber will convert this candidate and store ${output}. The source-quality label describes the download before conversion.`
                : 'Conversion is off, so MusicGrabber will keep the downloaded source format.';
            const conf = item.found_confidence != null ? Math.round(item.found_confidence * 100) + '%' : '';
            const verifiedBadge = item.found_verified
                ? '<span class="upgrade-verified" title="Quality confirmed by the source">verified</span>'
                : '<span class="upgrade-unverified" title="Estimated; confirmed only after download">needs download to confirm</span>';
            const canPreview = item.found_video_id && sourceHasPreview(item.found_source);
            const previewBtn = canPreview
                ? `<button class="btn btn-ghost btn-sm upgrade-preview"
                        onmouseenter="previewUpgrade('${item.found_video_id}', '${item.found_source}', this, ${item.id})"
                        onmouseleave="stopPreview()"
                        onclick="previewUpgrade('${item.found_video_id}', '${item.found_source}', this, ${item.id})">&#9654; Preview</button>`
                : '';
            cell.innerHTML = `
                <span class="upgrade-arrow">&rarr;</span>
                <span class="source-badge ${item.found_source}">${badge}</span>
                <span class="upgrade-quality-new">${sourceDescription}</span>
                <span class="upgrade-conversion-arrow" aria-hidden="true">&rarr;</span>
                <span class="upgrade-output-label" title="${escapeAttr(outputTitle)}">${outputDescription}</span>
                ${conf ? `<span class="upgrade-conf" title="Match confidence">${conf}</span>` : ''}
                ${verifiedBadge}
                ${previewBtn}
                <button class="btn btn-primary btn-sm upgrade-go" onclick="upgradeOne(${item.id}, this)" title="Download, verify, and swap in this copy">Upgrade</button>`;
        }

        // Walk the visible rows one at a time (~1/s) searching for a better copy.
        // Aborts cleanly if the token changes (user paginated away or left the tab).
        async function lazySearchUpgrades(items, token) {
            for (const item of items) {
                if (token !== upgradesSearchToken) return;
                if (item.found_searched) continue;  // already have a cached result rendered
                try {
                    const resp = await apiFetch(`/api/upgrades/candidates/${item.id}/search`, { method: 'POST' });
                    if (token !== upgradesSearchToken) return;
                    if (resp.ok) {
                        const updated = await resp.json();
                        renderProposedUpgrade(updated);
                    }
                } catch (e) { /* leave the row as-is */ }
                await new Promise(r => setTimeout(r, 1000));  // rate limit, be kind to sources
            }
        }

        function previewUpgrade(videoId, source, element, id) {
            // Reuse the search-result preview machinery with a synthetic result object.
            const row = document.getElementById(`upgrade-row-${id}`);
            let sourceUrl = null;
            // source_url isn't in the DOM; the preview endpoint needs it for url-based sources.
            // For those, fall back to the cached candidate data attached to the row.
            if (row && row._foundSourceUrl) sourceUrl = row._foundSourceUrl;
            startPreview(videoId, element, { source, source_url: sourceUrl });
        }

        async function upgradeOne(id, btn, force = false) {
            const row = document.getElementById(`upgrade-row-${id}`);
            const cell = document.getElementById(`upgrade-proposed-${id}`);
            if (btn) { btn.disabled = true; btn.textContent = force ? 'Forcing...' : 'Upgrading...'; }
            stopPreview();
            try {
                const url = `/api/upgrades/candidates/${id}/upgrade${force ? '?force=true' : ''}`;
                const resp = await apiFetch(url, { method: 'POST' });
                const data = await resp.json().catch(() => ({}));
                if (resp.ok && data.status === 'upgraded') {
                    // A successful upgrade can still sit below your target tier (the
                    // fetched copy was better, then convert-audio re-encoded it back
                    // down). The backend keeps such a row on the list, so removing it
                    // here would just make it "reappear" on the next load, looking
                    // like the upgrade silently failed when it did no such thing.
                    const stillBelow = data.still_below_target;
                    if (row) {
                        const c = row.querySelector('.upgrade-current .upgrade-meta');
                        if (c) {
                            c.innerHTML = stillBelow
                                ? `<span class="upgrade-verified">Upgraded</span> to ${escapeHtml(data.new_quality || '')}, still below your target &middot; old file in quarantine`
                                : '<span class="upgrade-verified">Upgraded</span> old file moved to quarantine';
                        }
                        if (!stillBelow) {
                            row.style.transition = 'opacity 0.4s';
                            row.style.opacity = '0.5';
                        }
                    }
                    if (cell) {
                        cell.innerHTML = stillBelow
                            ? '<span class="upgrade-none">Still below target; search again for a better copy</span>'
                            : '<span class="upgrade-verified">Done</span>';
                    }
                    if (stillBelow) {
                        showToast('Upgraded, but the result is still below your target quality');
                    } else {
                        showToast(force ? 'Force-upgraded; old file is in quarantine' : 'Upgraded; old file is in quarantine');
                        setTimeout(() => { if (row) row.remove(); if (!document.querySelectorAll('#watchedUpgradesList .upgrade-row').length) loadWatchedUpgrades(); }, 1200);
                    }
                } else if (data.status === 'rejected') {
                    // Offer a force override; the old file still goes to quarantine, so it's recoverable.
                    if (cell) cell.innerHTML = `<span class="upgrade-none">Rejected: ${escapeHtml(data.reason || 'failed checks')}</span>
                        <button class="btn btn-ghost btn-sm upgrade-force" onclick="forceUpgrade(${id}, this)" title="Swap it in anyway. The old file still goes to quarantine, so you can undo it.">Force anyway</button>`;
                    showToast('Upgrade rejected: ' + (data.reason || 'failed checks'), true);
                } else {
                    showToast('Upgrade failed: ' + (data.reason || resp.status), true);
                    if (btn) { btn.disabled = false; btn.textContent = force ? 'Force anyway' : 'Upgrade'; }
                }
            } catch (e) {
                showToast('Upgrade failed', true);
                if (btn) { btn.disabled = false; btn.textContent = force ? 'Force anyway' : 'Upgrade'; }
            }
        }

        function forceUpgrade(id, btn) {
            if (!confirm('Force this upgrade? The proposed copy failed a same-recording check (e.g. different length), so it may be a different version. The old file goes to quarantine and can be restored.')) return;
            upgradeOne(id, btn, true);
        }

        async function upgradeAll() {
            if (!confirm('Download and swap in every proposed upgrade on this page set? Old files are moved to quarantine (recoverable), not deleted.')) return;
            const btn = document.getElementById('upgradeAllBtn');
            try {
                const resp = await apiFetch('/api/upgrades/upgrade-all', { method: 'POST' });
                if (resp.ok) {
                    showToast('Upgrading all in the background; this can take a while.');
                    pollUpgradeAll(btn);
                } else if (resp.status === 400) {
                    showToast('Enable Track Upgrades in Settings first', true);
                }
            } catch (e) { showToast('Could not start upgrade-all', true); }
        }

        // Watch the background run to completion and reload the list ourselves.
        // The old behaviour was a toast saying "Refresh to see progress", which is
        // easy to miss and left a finished run looking like a stuck one.
        async function pollUpgradeAll(btn) {
            if (btn) btn.disabled = true;
            const originalLabel = btn ? btn.textContent : '';
            const restore = () => { if (btn) { btn.disabled = false; btn.textContent = originalLabel; } };
            let misses = 0;
            while (true) {
                await new Promise(r => setTimeout(r, 3000));
                let data;
                try {
                    const resp = await apiFetch('/api/upgrades/upgrade-all/status');
                    if (!resp.ok) { restore(); return; }
                    data = await resp.json();
                } catch (e) {
                    // A blip shouldn't abandon a run that's still going; several in a
                    // row probably means we're not getting it back.
                    if (++misses >= 3) { restore(); return; }
                    continue;
                }
                misses = 0;
                if (btn && data.total) btn.textContent = `Upgrading ${data.done}/${data.total}...`;
                if (!data.running) {
                    restore();
                    showToast(`Upgrade All finished: ${data.upgraded || 0} upgraded, ${data.rejected || 0} rejected, ${data.errored || 0} errored`);
                    upgradesPage = 1;
                    loadWatchedUpgrades();
                    return;
                }
            }
        }

        async function dismissUpgrade(id) {
            try {
                const resp = await apiFetch(`/api/upgrades/candidates/${id}/dismiss`, { method: 'POST' });
                if (resp.ok) {
                    const row = document.getElementById(`upgrade-row-${id}`);
                    if (row) row.remove();
                    if (!document.querySelectorAll('#watchedUpgradesList .upgrade-row').length) {
                        loadWatchedUpgrades();
                    }
                }
            } catch (e) { showToast('Could not dismiss', true); }
        }

        async function loadQuarantine() {
            const list = document.getElementById('quarantineList');
            if (!list) return;
            list.innerHTML = '<p class="watched-empty-text">Loading&hellip;</p>';
            try {
                const resp = await apiFetch('/api/upgrades/quarantine');
                if (!resp.ok) { list.innerHTML = '<p class="watched-empty-text">Could not load quarantine.</p>'; return; }
                const data = await resp.json();
                if (!data.items.length) {
                    list.innerHTML = '<p class="watched-empty-text">Nothing in quarantine.</p>';
                    return;
                }
                list.innerHTML = data.items.map(renderQuarantineRow).join('');
            } catch (e) {
                list.innerHTML = '<p class="watched-empty-text">Could not load quarantine.</p>';
            }
        }

        function renderQuarantineRow(item) {
            const filename = item.quarantine_path.split('/').pop();
            const title = escapeHtml(item.title || filename);
            const artist = escapeHtml(item.artist || '');
            const size = item.file_size ? formatBytes(item.file_size) : '';
            const when = formatDateYmd(item.quarantined_at);
            const restoreBtn = item.restorable
                ? `<button class="btn btn-primary btn-sm" onclick="restoreQuarantineEntry(${item.id}, this)" title="Put this file back where it lived; whatever replaced it is quarantined in turn">Restore</button>`
                : `<span class="upgrade-none" title="No original library location was recorded for this file">no restore info</span>`;
            // Orphaned entries (no numeric id) carry their filename via a data
            // attribute rather than an inline-JS string argument: filenames routinely
            // contain apostrophes (e.g. "Breakin' Outta Hell"), which would otherwise
            // break out of the quoted onclick argument.
            const deleteBtn = item.id != null
                ? `<button class="btn btn-danger btn-sm" onclick="deleteQuarantineEntry(${item.id}, this)" title="Delete this file for good">Delete</button>`
                : `<button class="btn btn-danger btn-sm" data-filename="${escapeAttr(filename)}" onclick="deleteOrphanedQuarantineFile(this)" title="Delete this file for good">Delete</button>`;
            return `
                <div class="result-item upgrade-row">
                    <div class="upgrade-current">
                        <div class="upgrade-track"><strong>${artist}</strong>${artist ? ' &ndash; ' : ''}${title}</div>
                        <div class="upgrade-meta">Quarantined ${when}${size ? ' · ' + size : ''}</div>
                    </div>
                    <div class="upgrade-actions">
                        ${restoreBtn}
                        ${deleteBtn}
                    </div>
                </div>`;
        }

        async function restoreQuarantineEntry(id, btn) {
            if (!confirm('Restore this file? Whatever replaced it will be quarantined in turn, so this can be undone too.')) return;
            if (btn) btn.disabled = true;
            try {
                const resp = await apiFetch(`/api/upgrades/quarantine/${id}/restore`, { method: 'POST' });
                const data = await resp.json().catch(() => ({}));
                if (resp.ok && data.status === 'restored') {
                    showToast('Restored');
                    loadQuarantine();
                    if (!document.querySelectorAll('#watchedUpgradesList .upgrade-row').length) loadWatchedUpgrades();
                } else {
                    showToast('Restore failed: ' + (data.reason || resp.status), true);
                    if (btn) btn.disabled = false;
                }
            } catch (e) {
                showToast('Restore failed', true);
                if (btn) btn.disabled = false;
            }
        }

        async function deleteQuarantineEntry(id, btn) {
            if (!confirm('Delete this file for good? There is no undo.')) return;
            if (btn) btn.disabled = true;
            try {
                const resp = await apiFetch(`/api/upgrades/quarantine/${id}`, { method: 'DELETE' });
                if (resp.ok) {
                    showToast('Deleted');
                    loadQuarantine();
                } else {
                    const data = await resp.json().catch(() => ({}));
                    showToast('Delete failed: ' + (data.detail || resp.status), true);
                    if (btn) btn.disabled = false;
                }
            } catch (e) {
                showToast('Delete failed', true);
                if (btn) btn.disabled = false;
            }
        }

        async function deleteOrphanedQuarantineFile(btn) {
            if (!confirm('Delete this file for good? There is no undo, and its original library location is not known.')) return;
            const filename = btn.dataset.filename;
            btn.disabled = true;
            try {
                const resp = await apiFetch(`/api/upgrades/quarantine/orphaned?filename=${encodeURIComponent(filename)}`, { method: 'DELETE' });
                if (resp.ok) {
                    showToast('Deleted');
                    loadQuarantine();
                } else {
                    const data = await resp.json().catch(() => ({}));
                    showToast('Delete failed: ' + (data.detail || resp.status), true);
                    if (btn) btn.disabled = false;
                }
            } catch (e) {
                showToast('Delete failed', true);
                if (btn) btn.disabled = false;
            }
        }

        // =====================================================================
        // Read-only Audio Provenance Audit
        // =====================================================================
        const AUDIO_AUDIT_PER_PAGE = 25;
        const AUDIO_AUDIT_CLASSIFICATIONS = {
            known_lossy_transcode: 'Known lossy transcode',
            native_lossy: 'Native lossy acquisition',
            lossy_derivative: 'Lossy derivative of recorded lossless',
            recorded_lossless: 'Recorded lossless acquisition',
            historical_unknown: 'Historical or unknown provenance',
            unreadable: 'Unreadable audio file',
        };
        const AUDIO_AUDIT_QUALITY_LABELS = {
            recorded_lossless: 'Recorded lossless',
            lossy_320: 'Lossy, up to 320 tier',
            lossy_256: 'Lossy, up to 256 tier',
            lossy_192: 'Lossy, up to 192 tier',
            lossy_128: 'Lossy, up to 128 tier',
            lossy_unknown: 'Lossy, bitrate unknown',
            unknown: 'Unknown',
        };
        let audioAuditPage = 1;
        let audioAuditPollTimer = null;
        let audioAuditLastStatus = null;

        function audioAuditFilters() {
            return {
                classification: document.getElementById('audioAuditClassification')?.value || '',
                container: document.getElementById('audioAuditContainer')?.value || '',
                codec: document.getElementById('audioAuditCodec')?.value || '',
                source: document.getElementById('audioAuditSource')?.value || '',
                effective_quality: document.getElementById('audioAuditEffective')?.value || '',
                lossless_only: document.getElementById('audioAuditLossless')?.value || '',
            };
        }

        function audioAuditQuery(includeFormat = null) {
            const params = new URLSearchParams();
            Object.entries(audioAuditFilters()).forEach(([key, value]) => {
                if (value) params.set(key, value);
            });
            if (includeFormat) params.set('format', includeFormat);
            return params.toString();
        }

        function fillAudioAuditSelect(id, values, labelForValue = value => value) {
            const select = document.getElementById(id);
            if (!select) return;
            const selected = select.value;
            const first = select.options[0];
            const placeholder = first ? first.textContent : 'All';
            select.innerHTML = `<option value="">${escapeHtml(placeholder)}</option>` +
                (values || []).map(value =>
                    `<option value="${escapeHtml(String(value))}">${escapeHtml(String(labelForValue(value)))}</option>`
                ).join('');
            if ([...select.options].some(option => option.value === selected)) {
                select.value = selected;
            }
        }

        function populateAudioAuditFilters(status) {
            fillAudioAuditSelect(
                'audioAuditClassification',
                Object.keys(AUDIO_AUDIT_CLASSIFICATIONS),
                value => AUDIO_AUDIT_CLASSIFICATIONS[value]
            );
            const options = status.options || {};
            fillAudioAuditSelect('audioAuditContainer', options.container || [], value => String(value).toUpperCase());
            fillAudioAuditSelect('audioAuditCodec', options.codec || [], value => String(value).toUpperCase());
            fillAudioAuditSelect('audioAuditSource', options.source || []);
            fillAudioAuditSelect(
                'audioAuditEffective',
                options.effective_quality || [],
                value => AUDIO_AUDIT_QUALITY_LABELS[value] || value
            );
        }

        function renderAudioAuditSummary(summary) {
            const element = document.getElementById('audioAuditSummary');
            if (!element) return;
            if (!summary) {
                element.innerHTML = '';
                return;
            }
            const counts = summary.counts || {};
            const ordered = [
                'known_lossy_transcode',
                'native_lossy',
                'lossy_derivative',
                'recorded_lossless',
                'historical_unknown',
                'unreadable',
            ];
            element.innerHTML = ordered.map(key => `
                <div class="audio-audit-summary-card audit-${key}">
                    <span class="audio-audit-summary-count">${Number(counts[key] || 0).toLocaleString()}</span>
                    <span>${escapeHtml(AUDIO_AUDIT_CLASSIFICATIONS[key])}</span>
                </div>
            `).join('');
        }

        function renderAudioAuditStatus(status) {
            const element = document.getElementById('audioAuditStatus');
            const runButton = document.getElementById('runAudioAuditBtn');
            if (!element) return;
            const running = status.running;
            const current = status.current;
            if (runButton) {
                runButton.disabled = !!running;
                runButton.textContent = running ? 'Auditing...' : 'Run Audit';
            }
            if (running) {
                const scanned = Number(running.scanned_files || 0).toLocaleString();
                const total = Number(running.total_files || 0).toLocaleString();
                if (!running.total_files) {
                    element.innerHTML = '<strong>Discovering:</strong> walking the configured music directory for supported audio files. Nothing is being changed.';
                    return;
                }
                const progress = running.total_files ? ` (${running.progress_percent || 0}%)` : '';
                const oldReport = current ? ' The previous complete report remains below until this finishes.' : '';
                element.innerHTML = `<strong>Scanning:</strong> ${scanned} / ${total} files${progress}.${escapeHtml(oldReport)}`;
                return;
            }
            if (current) {
                const when = current.completed_at ? new Date(`${current.completed_at}Z`).toLocaleString() : 'unknown time';
                element.innerHTML = `<strong>Complete:</strong> ${Number(current.total_files || 0).toLocaleString()} supported audio files inspected at ${escapeHtml(when)}. ${escapeHtml(status.read_only_notice || '')}`;
                return;
            }
            if (status.last_failed) {
                element.innerHTML = `<strong>Last audit failed:</strong> ${escapeHtml(status.last_failed.error || 'Unknown error')}. No previous report was replaced.`;
                return;
            }
            element.textContent = 'No audit report yet. Run one when you fancy learning what the tags actually remember.';
        }

        function renderAudioAuditRow(item) {
            const classification = AUDIO_AUDIT_CLASSIFICATIONS[item.classification] || item.classification;
            const effective = AUDIO_AUDIT_QUALITY_LABELS[item.effective_quality] || item.effective_quality;
            const storedParts = [
                item.container ? String(item.container).toUpperCase() + ' container' : '',
                item.codec ? String(item.codec).toUpperCase() + ' codec' : '',
                item.bitrate_kbps ? `${item.bitrate_kbps} kbps observed` : '',
            ].filter(Boolean);
            const identity = [item.artist, item.title].filter(Boolean).join(' – ');
            const evidence = (item.evidence || []).map(entry => `
                <li>
                    <strong>${escapeHtml(String(entry.label || entry.field || 'Evidence'))}:</strong>
                    ${escapeHtml(String(entry.value || ''))}
                    <span>${escapeHtml(String(entry.meaning || ''))}</span>
                </li>
            `).join('');
            const caveats = (item.caveats || []).map(caveat =>
                `<li>${escapeHtml(String(caveat))}</li>`
            ).join('');
            return `
                <div class="result-item audio-audit-row">
                    <div class="audio-audit-main">
                        <div class="audio-audit-path">${escapeHtml(String(item.path || item.filename || 'Unknown file'))}</div>
                        ${identity ? `<div class="audio-audit-identity">${escapeHtml(identity)}</div>` : ''}
                        <div class="audio-audit-meta">${escapeHtml(storedParts.join(' · ') || 'Stored format unknown')}</div>
                        <div class="audio-audit-meta">
                            Effective: <strong>${escapeHtml(String(effective || 'Unknown'))}</strong>
                            ${item.source ? ` · Acquisition source: ${escapeHtml(String(item.source))}` : ''}
                        </div>
                    </div>
                    <div class="audio-audit-result">
                        <span class="audio-audit-badge audit-${escapeHtml(String(item.classification))}">${escapeHtml(String(classification))}</span>
                        <details class="audio-audit-evidence">
                            <summary>Why this classification?</summary>
                            <h4>Evidence used</h4>
                            <ul>${evidence}</ul>
                            <h4>Caveats</h4>
                            <ul class="audio-audit-caveats">${caveats}</ul>
                        </details>
                    </div>
                </div>
            `;
        }

        async function loadAudioAudit(refreshFilters = true) {
            const list = document.getElementById('audioAuditList');
            const pager = document.getElementById('audioAuditPager');
            if (!list || isPeon()) return;
            if (audioAuditPollTimer) {
                clearTimeout(audioAuditPollTimer);
                audioAuditPollTimer = null;
            }
            try {
                const statusResponse = await apiFetch('/api/audio-audit');
                if (!statusResponse.ok) throw new Error('Could not load audit status');
                const status = await statusResponse.json();
                audioAuditLastStatus = status;
                renderAudioAuditStatus(status);
                renderAudioAuditSummary(status.summary);
                if (refreshFilters) populateAudioAuditFilters(status);

                if (!status.current) {
                    list.innerHTML = status.running
                        ? '<p class="watched-empty-text">Building the first report. Audio files are being read, not touched.</p>'
                        : '<p class="watched-empty-text">No completed audit report yet.</p>';
                    if (pager) pager.style.display = 'none';
                } else {
                    const query = audioAuditQuery();
                    const separator = query ? '&' : '';
                    const filesResponse = await apiFetch(
                        `/api/audio-audit/files?page=${audioAuditPage}&per_page=${AUDIO_AUDIT_PER_PAGE}${separator}${query}`
                    );
                    if (!filesResponse.ok) throw new Error('Could not load audit files');
                    const data = await filesResponse.json();
                    if (!data.items.length) {
                        list.innerHTML = '<p class="watched-empty-text">No files match these filters.</p>';
                    } else {
                        list.innerHTML = data.items.map(renderAudioAuditRow).join('');
                    }
                    if (pager) {
                        const info = document.getElementById('audioAuditPageInfo');
                        if (info) info.textContent = `Page ${data.page} of ${data.pages} (${Number(data.total || 0).toLocaleString()} files)`;
                        document.getElementById('audioAuditPrevBtn').disabled = data.page <= 1;
                        document.getElementById('audioAuditNextBtn').disabled = data.page >= data.pages;
                        pager.style.display = data.pages > 1 ? 'flex' : 'none';
                    }
                }

                if (status.running && currentTab === 'watched') {
                    audioAuditPollTimer = setTimeout(() => loadAudioAudit(false), 2000);
                }
            } catch (error) {
                list.innerHTML = `<p class="watched-empty-text">Could not load the audit: ${escapeHtml(error.message || 'unknown error')}</p>`;
            }
        }

        async function runAudioAudit() {
            const button = document.getElementById('runAudioAuditBtn');
            if (button) {
                button.disabled = true;
                button.textContent = 'Starting...';
            }
            try {
                const response = await apiFetch('/api/audio-audit/scans', { method: 'POST' });
                const data = await response.json().catch(() => ({}));
                if (!response.ok) throw new Error(data.detail || 'Could not start audit');
                audioAuditPage = 1;
                showToast(data.status === 'already_running' ? 'An audio audit is already running' : 'Audio audit started');
                loadAudioAudit();
            } catch (error) {
                showToast(`Could not start audio audit: ${error.message}`, true);
                if (button) {
                    button.disabled = false;
                    button.textContent = 'Run Audit';
                }
            }
        }

        function audioAuditFiltersChanged() {
            audioAuditPage = 1;
            loadAudioAudit(false);
        }

        function changeAudioAuditPage(delta) {
            audioAuditPage = Math.max(1, audioAuditPage + delta);
            loadAudioAudit(false);
            document.getElementById('audioAudit')?.scrollIntoView({ behavior: 'smooth' });
        }

        async function exportAudioAudit(format) {
            if (!audioAuditLastStatus?.current) {
                showToast('Run an audio audit before exporting it', true);
                return;
            }
            try {
                const response = await apiFetch(`/api/audio-audit/export?${audioAuditQuery(format)}`);
                if (!response.ok) throw new Error('Export failed');
                const blob = await response.blob();
                const disposition = response.headers.get('Content-Disposition') || '';
                const match = disposition.match(/filename="([^"]+)"/);
                const filename = match ? match[1] : `musicgrabber-audio-audit.${format}`;
                const url = URL.createObjectURL(blob);
                const link = document.createElement('a');
                link.href = url;
                link.download = filename;
                document.body.appendChild(link);
                link.click();
                link.remove();
                URL.revokeObjectURL(url);
            } catch (error) {
                showToast('Could not export the audio audit', true);
            }
        }

        async function loadWatchedArtists(showLoading = true) {
            const listEl = document.getElementById('watchedArtistList');
            if (showLoading) listEl.innerHTML = '<div class="loading"><div class="spinner"></div></div>';
            try {
                const res = await apiFetch('/api/watched-artists');
                const data = await res.json();
                renderWatchedArtists(data.artists || []);
            } catch (e) {
                listEl.innerHTML = '<div class="empty-state"><p>Failed to load watched artists</p></div>';
            }
        }

        function renderWatchedArtists(artists) {
            const listEl = document.getElementById('watchedArtistList');
            if (!artists || artists.length === 0) {
                stopArtistRefreshPolling();
                listEl.innerHTML = '<div class="empty-state" style="padding: 1.5rem 0;"><div class="empty-state-icon"><i class="fa-solid fa-heart-circle-plus"></i></div><p>No artists followed yet</p></div>';
                const albumPanel = document.getElementById('artistAlbumPanel');
                if (albumPanel) albumPanel.style.display = 'none';
                return;
            }
            let anyRunning = false;
            watchedArtistsById = new Map(artists.map(a => [a.id, a]));
            const singlesOnlyMode = !!(serverConfig && serverConfig.singles_only_mode);
            listEl.innerHTML = artists.map(artist => {
                // Reconcile the optimistic "pending" flag against backend state.
                // Once the backend reports the refresh as running it's authoritative,
                // so drop the optimistic flag (otherwise it would linger after the
                // refresh finishes and spin the card forever). A safety timeout
                // covers refreshes so quick we never caught them "running".
                const pend = artistRefreshPending.get(artist.id);
                if (pend) {
                    const pendAgeMs = Date.now() - new Date(pend.startedAt).getTime();
                    if (artist.refresh_state === 'running' || pendAgeMs > 20000) {
                        artistRefreshPending.delete(artist.id);
                    }
                }
                const isRunning = artist.refresh_state === 'running' || artistRefreshPending.has(artist.id);
                if (isRunning) anyRunning = true;
                const isError = artist.refresh_state === 'error';
                const isPaused = !artist.enabled;
                // Older rows predate the column; absent means singles, as it always did.
                const watchesSingles = artist.watch_singles === undefined || !!artist.watch_singles;
                const intervalLabel = artist.refresh_interval_hours >= 720 ? 'monthly'
                    : artist.refresh_interval_hours >= 168 ? 'weekly'
                    : artist.refresh_interval_hours >= 24 ? 'daily'
                    : artist.refresh_interval_hours >= 12 ? 'every 12h'
                    : artist.refresh_interval_hours >= 6 ? 'every 6h'
                    : artist.refresh_interval_hours >= 1 ? 'hourly'
                    : 'every 30min';
                const lastChecked = artist.last_checked
                    ? `Last checked: <span title="${escapeAttr(formatTimeExact(artist.last_checked))}">${escapeHtml(formatTimeAgo(artist.last_checked))}</span>`
                    : 'Never checked';
                let stageHtml = '';
                if (isRunning) {
                    const pending = artistRefreshPending.get(artist.id);
                    const startedAt = pending?.startedAt || artist.refresh_started_at;
                    const elapsed = startedAt ? Math.floor((Date.now() - new Date(startedAt + 'Z').getTime()) / 1000) : 0;
                    const stageLabel = {
                        starting: 'Starting', fetching: 'Fetching from MusicBrainz',
                        diffing: 'Comparing tracks', queueing: 'Queueing downloads', done: 'Done',
                    }[artist.refresh_stage] || 'Refreshing';
                    stageHtml = `<span class="watched-card-refreshing"><span class="watched-refresh-spinner"></span>${escapeHtml(stageLabel)}${elapsed > 0 ? ` (${elapsed}s)` : ''}</span>`;
                }
                return `
                <div class="watched-card" id="artist-card-${artist.id}">
                    <div class="watched-card-header">
                        <span><i class="fa-brands fa-creative-commons-sampling"></i></span>
                        <span class="watched-card-name">${escapeHtml(artist.name)}</span>
                        ${stageHtml}
                        ${watchesSingles ? '<span class="watched-card-mode">Singles</span>' : ''}
                        ${artist.auto_add_albums ? '<span class="watched-card-mode">Albums</span>' : ''}
                        ${isPaused ? '<span class="watched-card-paused">Paused</span>' : ''}
                    </div>
                    <div class="watched-card-meta">
                        ${watchesSingles
                            ? `${artist.tracked_count || 0} singles tracked &middot; ${artist.downloaded_count || 0} downloaded &middot; `
                            : 'Albums only &middot; '}${intervalLabel} &middot; ${lastChecked}
                    </div>
                    ${isError ? `<div class="watched-card-refresh-error"><i class="fa-solid fa-circle-exclamation"></i><span>${escapeHtml(artist.refresh_error || 'Refresh failed')}</span></div>` : ''}
                    ${artist.unresolved_tracks > 0 ? `
                    <div class="watched-card-stale-warning">
                        <i class="fa-solid fa-circle-info"></i>
                        <span><strong>${artist.unresolved_tracks} downloaded single${artist.unresolved_tracks === 1 ? '' : 's'}</strong> can no longer be found on disk. Re-download Missing Files is off, so ${artist.unresolved_tracks === 1 ? 'it is' : 'they are'} being left alone instead of fetched again.</span>
                    </div>` : ''}
                    <div class="watched-card-settings">
                        <label class="watched-card-toggle" title="Follow this artist's new singles. Switch off to keep them as an albums-only follow.">
                            Singles
                            <div class="toggle-switch">
                                <input type="checkbox" id="artist-singles-toggle-${artist.id}" ${watchesSingles ? 'checked' : ''}
                                    onchange="updateArtistSingles('${artist.id}', this.checked)">
                                <span class="toggle-slider"></span>
                            </div>
                        </label>
                        ${watchesSingles ? `
                        <label class="watched-card-toggle" title="Singles released before this date are ignored. Move it back to pick up older releases, forward to quieten things down.">
                            From:
                            <input type="date" class="watched-card-date" value="${escapeAttr(artist.from_date || '')}"
                                onchange="updateArtistFromDate('${artist.id}', this.value)">
                        </label>` : ''}
                        <label class="watched-card-toggle" title="Choose whether new singles keep their source format or are converted">
                            ${conversionChoiceLabel(artist.convert_audio)}
                            <div class="toggle-switch">
                                <input type="checkbox" ${artist.convert_audio ? 'checked' : ''}
                                    onchange="updateArtistConversion('${artist.id}', this.checked)">
                                <span class="toggle-slider"></span>
                            </div>
                        </label>
                        <label class="watched-card-toggle">
                            Check:
                            <select class="watched-card-select" onchange="updateArtistInterval('${artist.id}', this.value)">
                                <option value="0.5" ${artist.refresh_interval_hours == 0.5 ? 'selected' : ''}>Every 30 min</option>
                                <option value="1" ${artist.refresh_interval_hours == 1 ? 'selected' : ''}>Hourly</option>
                                <option value="6" ${artist.refresh_interval_hours == 6 ? 'selected' : ''}>Every 6 hours</option>
                                <option value="12" ${artist.refresh_interval_hours == 12 ? 'selected' : ''}>Every 12 hours</option>
                                <option value="24" ${artist.refresh_interval_hours == 24 ? 'selected' : ''}>Daily</option>
                                <option value="168" ${artist.refresh_interval_hours == 168 ? 'selected' : ''}>Weekly</option>
                                <option value="720" ${artist.refresh_interval_hours == 720 ? 'selected' : ''}>Monthly</option>
                            </select>
                        </label>
                    </div>
                    <div id="artist-singles-start-${artist.id}" class="watched-singles-start" style="display:none;"></div>
                    <div class="watched-card-actions">
                        <button class="watched-action-btn" type="button"
                            data-action="refresh-watched-artist"
                            data-artist-id="${escapeAttr(artist.id)}"
                            ${isRunning ? 'disabled' : ''}>Refresh</button>
                        <button class="watched-action-btn" type="button"
                            data-action="toggle-artist-missing-tracks"
                            data-artist-id="${escapeAttr(artist.id)}">Missing</button>
                        <button class="watched-action-btn" type="button"
                            data-action="toggle-artist-track-list"
                            data-artist-id="${escapeAttr(artist.id)}"
                            data-artist-name="${escapeAttr(artist.name)}">Tracks</button>
                        ${singlesOnlyMode ? '' : `<button class="watched-action-btn" type="button"
                            data-action="toggle-artist-albums"
                            data-artist-id="${escapeAttr(artist.id)}">Albums</button>`}
                        <button class="watched-action-btn watched-action-pause" type="button"
                            data-action="toggle-watched-artist"
                            data-artist-id="${escapeAttr(artist.id)}"
                            data-enabled="${artist.enabled ? 'true' : 'false'}">${isPaused ? 'Resume' : 'Pause'}</button>
                        <button class="watched-action-btn watched-action-delete" type="button"
                            data-action="delete-watched-artist"
                            data-artist-id="${escapeAttr(artist.id)}"
                            data-artist-name="${escapeAttr(artist.name)}">Delete</button>
                    </div>
                    <div id="artist-missing-${artist.id}" class="watched-card-expanded" style="display:none;"></div>
                    <div id="artist-tracks-${artist.id}" class="watched-card-expanded" style="display:none;"></div>
                </div>`;
            }).join('');

            if (anyRunning) {
                startArtistRefreshPolling();
            } else {
                stopArtistRefreshPolling();
                artistRefreshPending.clear();
            }
        }

        async function refreshWatchedArtist(artistId) {
            artistRefreshPending.set(artistId, { stage: 'starting', startedAt: new Date().toISOString() });
            loadWatchedArtists(false);
            try {
                const res = await apiFetch(`/api/watched-artists/${artistId}/refresh`, { method: 'POST' });
                const data = await res.json();
                if (data.already_running) {
                    showToast('Refresh already in progress');
                } else {
                    // Refresh runs in the background now; the card polls for progress.
                    showToast('Refresh started');
                }
            } catch (e) {
                showToast('Refresh failed', true);
                artistRefreshPending.delete(artistId);
            } finally {
                loadWatchedArtists();
            }
        }

        async function toggleWatchedArtist(artistId, enabled) {
            try {
                await apiFetch(`/api/watched-artists/${artistId}`, {
                    method: 'PUT',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ enabled })
                });
                loadWatchedArtists();
                showToast(enabled ? 'Artist watching resumed' : 'Artist watching paused');
            } catch (e) {
                showToast('Failed to update artist', true);
            }
        }

        async function updateArtistConversion(artistId, convertAudio) {
            try {
                await apiFetch(`/api/watched-artists/${artistId}`, {
                    method: 'PUT',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ convert_audio: convertAudio })
                });
            } catch (e) {
                showToast('Failed to update format setting', true);
                loadWatchedArtists();
            }
        }

        // Switching singles ON needs a date before it does anything, because the
        // stored from_date on an albums-only follow is simply the day you followed
        // them, which could be a year of singles ago. Ask first, send nothing until
        // asked; switching OFF is harmless and goes straight through.
        function updateArtistSingles(artistId, watchSingles) {
            const panel = document.getElementById(`artist-singles-start-${artistId}`);
            if (!watchSingles || !panel) return _sendArtistSingles(artistId, watchSingles);

            const today = new Date().toISOString().slice(0, 10);
            panel.style.display = 'block';
            panel.innerHTML = `
                <span class="watched-singles-start-label">Watch singles released from:</span>
                <input type="date" class="watched-card-date" id="artist-singles-from-${artistId}" value="${today}">
                <button class="watched-action-btn" type="button"
                    onclick="confirmArtistSingles('${artistId}')">Start watching</button>
                <button class="watched-action-btn" type="button"
                    onclick="cancelArtistSingles('${artistId}')">Cancel</button>`;
            document.getElementById(`artist-singles-from-${artistId}`)?.focus();
        }

        function cancelArtistSingles(artistId) {
            const panel = document.getElementById(`artist-singles-start-${artistId}`);
            if (panel) { panel.style.display = 'none'; panel.innerHTML = ''; }
            // Put the toggle back where it was, since nothing was actually saved.
            const toggle = document.getElementById(`artist-singles-toggle-${artistId}`);
            if (toggle) toggle.checked = false;
        }

        function confirmArtistSingles(artistId) {
            const input = document.getElementById(`artist-singles-from-${artistId}`);
            const fromDate = input ? input.value : '';
            if (!fromDate) {
                showToast('Pick a date first, otherwise there is nothing to start from.', true);
                return;
            }
            return _sendArtistSingles(artistId, true, fromDate);
        }

        async function updateArtistFromDate(artistId, fromDate) {
            if (!fromDate) return;
            try {
                const res = await apiFetch(`/api/watched-artists/${artistId}`, {
                    method: 'PUT',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ from_date: fromDate })
                });
                if (!res.ok) throw new Error('rejected');
                showToast(`Now watching singles released from ${fromDate}`);
            } catch (e) {
                showToast('Failed to update the singles start date', true);
            }
            loadWatchedArtists();
        }

        async function _sendArtistSingles(artistId, watchSingles, fromDate) {
            try {
                const payload = { watch_singles: watchSingles };
                if (fromDate) payload.from_date = fromDate;
                const res = await apiFetch(`/api/watched-artists/${artistId}`, {
                    method: 'PUT',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(payload)
                });
                // The server refuses to leave an artist following nothing at all,
                // so surface that rather than silently bouncing the toggle back.
                if (!res.ok) {
                    let detail = 'Failed to update singles following';
                    try { detail = (await res.json()).detail || detail; } catch (_) { /* keep the default */ }
                    showToast(detail, true);
                }
            } catch (e) {
                showToast('Failed to update singles following', true);
            }
            loadWatchedArtists();
        }

        async function updateArtistInterval(artistId, hours) {
            try {
                await apiFetch(`/api/watched-artists/${artistId}`, {
                    method: 'PUT',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ refresh_interval_hours: parseFloat(hours) })
                });
            } catch (e) {
                showToast('Failed to update interval', true);
                loadWatchedArtists();
            }
        }

        async function deleteWatchedArtist(artistId, name) {
            if (!confirm(`Stop watching "${name}"? Downloaded tracks will not be deleted.`)) return;
            try {
                await apiFetch(`/api/watched-artists/${artistId}`, { method: 'DELETE' });
                showToast(`Stopped watching "${name}"`);
                loadWatchedArtists();
            } catch (e) {
                showToast('Failed to delete artist', true);
            }
        }

        async function toggleArtistMissingTracks(artistId) {
            const panel = document.getElementById(`artist-missing-${artistId}`);
            if (panel.style.display !== 'none') { panel.style.display = 'none'; return; }
            panel.style.display = 'block';
            panel.innerHTML = '<div class="loading"><div class="spinner"></div></div>';
            try {
                const res = await apiFetch(`/api/watched-artists/${artistId}/missing`);
                const data = await res.json();
                if (!data.tracks || data.tracks.length === 0) {
                    panel.innerHTML = '<p style="font-size:12px;color:var(--text-secondary);padding:8px 0;">No missing singles.</p>';
                    return;
                }
                panel.innerHTML = `
                    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
                        <span style="font-size:12px;color:var(--text-secondary);">${data.tracks.length} missing single(s)</span>
                        <button type="button"
                            data-action="retry-all-artist-missing"
                            data-artist-id="${escapeAttr(artistId)}"
                            style="padding:4px 12px;font-size:12px;font-family:inherit;font-weight:600;background:var(--accent);color:#fff;border:none;border-radius:6px;cursor:pointer;">
                            Queue All
                        </button>
                    </div>` +
                    data.tracks.map(t => `
                        <div style="padding:4px 0;display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap;">
                            <span style="flex:1;min-width:0;font-size:12px;">${escapeHtml(t.artist || '')} &ndash; ${escapeHtml(t.title)}</span>
                            <div style="display:flex;gap:4px;align-items:center;flex-shrink:0;">
                                ${t.release_date ? `<span style="font-size:11px;color:var(--text-secondary);white-space:nowrap;">${t.release_date}</span>` : ''}
                                <button type="button"
                                    data-action="retry-artist-track"
                                    data-artist-id="${escapeAttr(artistId)}"
                                    data-artist="${escapeAttr(t.artist || '')}"
                                    data-title="${escapeAttr(t.title)}"
                                    style="padding:3px 8px;font-size:11px;font-family:inherit;background:var(--bg-tertiary);color:var(--text-secondary);border:1px solid var(--border);border-radius:4px;cursor:pointer;white-space:nowrap;"
                                    title="Auto-search and re-queue this track">Retry</button>
                            </div>
                        </div>`).join('');
            } catch (e) {
                panel.innerHTML = '<p style="font-size:12px;color:var(--error);">Failed to load missing tracks.</p>';
            }
        }

        const ARTIST_TRACKS_PAGE_SIZE = 50;

        async function toggleArtistTrackList(artistId, artistName) {
            const panel = document.getElementById(`artist-tracks-${artistId}`);
            if (panel.style.display !== 'none') { panel.style.display = 'none'; return; }
            panel.style.display = 'block';
            await loadArtistTrackPage(artistId, 0);
        }

        // Prolific artists run to hundreds of singles, so the track list is paged
        // ('cycles'). Prev/Next reload the panel at a new offset.
        async function loadArtistTrackPage(artistId, offset) {
            const panel = document.getElementById(`artist-tracks-${artistId}`);
            if (!panel) return;
            offset = Math.max(0, offset);
            panel.innerHTML = '<div class="loading"><div class="spinner"></div></div>';
            try {
                const res = await apiFetch(`/api/watched-artists/${artistId}/tracks?limit=${ARTIST_TRACKS_PAGE_SIZE}&offset=${offset}`);
                const data = await res.json();
                const total = data.total || 0;
                if (total === 0) {
                    panel.innerHTML = '<p style="font-size:12px;color:var(--text-secondary);padding:8px 0;">No tracks tracked yet.</p>';
                    return;
                }
                const tracksHtml = (data.tracks || []).map(t => {
                    const statusIcon = t.downloaded_at
                        ? '<i class="fa-solid fa-check" style="color:var(--success);"></i>'
                        : t.job_status === 'queued' || t.job_status === 'downloading'
                            ? '<i class="fa-solid fa-clock" style="color:var(--text-secondary);"></i>'
                            : '<i class="fa-solid fa-xmark" style="color:var(--error);"></i>';
                    const dlBtn = t.downloaded_at && t.job_id
                        ? `<button type="button"
                            data-action="save-job-to-device"
                            data-job-id="${escapeAttr(t.job_id)}"
                            style="padding:2px 8px;font-size:11px;font-family:inherit;background:var(--bg-tertiary);color:var(--text-secondary);border:1px solid var(--border);border-radius:4px;cursor:pointer;white-space:nowrap;"><i class="fa-solid fa-download"></i></button>`
                        : '';
                    return `<div style="display:flex;align-items:center;gap:8px;padding:3px 0;font-size:12px;">
                        ${statusIcon}
                        <span style="flex:1;">${escapeHtml(t.artist || '')} &ndash; ${escapeHtml(t.title)}</span>
                        ${t.release_date ? `<span style="color:var(--text-secondary);font-size:11px;">${t.release_date}</span>` : ''}
                        ${dlBtn}
                    </div>`;
                }).join('');

                const start = offset + 1;
                const end = Math.min(offset + ARTIST_TRACKS_PAGE_SIZE, total);
                const hasPrev = offset > 0;
                const hasNext = end < total;
                const btnStyle = 'padding:3px 10px;font-size:11px;font-family:inherit;background:var(--bg-tertiary);color:var(--text-primary);border:1px solid var(--border);border-radius:4px;cursor:pointer;';
                const pager = total > ARTIST_TRACKS_PAGE_SIZE
                    ? `<div style="display:flex;align-items:center;justify-content:space-between;gap:8px;margin-top:8px;">
                            <button type="button" ${hasPrev ? '' : 'disabled'} onclick="loadArtistTrackPage('${artistId}', ${offset - ARTIST_TRACKS_PAGE_SIZE})" style="${btnStyle}${hasPrev ? '' : 'opacity:0.4;cursor:default;'}">&larr; Prev</button>
                            <span style="font-size:11px;color:var(--text-secondary);">${start}&ndash;${end} of ${total}</span>
                            <button type="button" ${hasNext ? '' : 'disabled'} onclick="loadArtistTrackPage('${artistId}', ${offset + ARTIST_TRACKS_PAGE_SIZE})" style="${btnStyle}${hasNext ? '' : 'opacity:0.4;cursor:default;'}">Next &rarr;</button>
                        </div>`
                    : '';

                panel.innerHTML = `<div style="font-size:12px;color:var(--text-secondary);margin-bottom:8px;">${total} single(s) tracked:</div>` + tracksHtml + pager;
            } catch (e) {
                panel.innerHTML = '<p style="font-size:12px;color:var(--error);">Failed to load tracks.</p>';
            }
        }

        async function retryArtistTrack(artistId, artist, title, btn) {
            btn.disabled = true;
            btn.textContent = 'Queued';
            try {
                await apiFetch(`/api/watched-artists/${artistId}/retry-track`, {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ artist, title })
                });
                showToast(`Queued: ${artist} - ${title}`);
            } catch (e) {
                btn.disabled = false;
                btn.textContent = 'Retry';
                showToast('Retry failed', true);
            }
        }

        async function retryAllArtistMissing(artistId, btn) {
            btn.disabled = true;
            btn.textContent = 'Queuing...';
            try {
                const res = await apiFetch(`/api/watched-artists/${artistId}/retry-all-missing`, { method: 'POST' });
                const data = await res.json();
                showToast(`Queued ${data.queued} track(s)`);
                btn.textContent = `${data.queued} queued`;
                // Disable all individual retry buttons too
                const panel = document.getElementById(`artist-missing-${artistId}`);
                if (panel) panel.querySelectorAll('button[data-action="retry-artist-track"]').forEach(b => { b.disabled = true; b.textContent = 'Queued'; });
            } catch (e) {
                btn.disabled = false;
                btn.textContent = 'Queue All';
                showToast('Failed to queue tracks', true);
            }
        }

        async function refreshAllArtists() {
            const btn = document.getElementById('refreshAllArtistsBtn');
            btn.disabled = true;
            try {
                const res = await apiFetch('/api/watched-artists/check-all', { method: 'POST' });
                const data = await res.json();
                if (data.checked === 0) {
                    showToast('No artists due for refresh');
                } else {
                    showToast(`Checked ${data.checked} artist(s), ${data.total_new_tracks} new single(s) found`);
                }
                loadWatchedArtists();
            } catch (e) {
                showToast('Check failed', true);
            } finally {
                btn.disabled = false;
            }
        }

        // Event listeners for watched artists
        document.getElementById('searchArtistBtn').addEventListener('click', searchArtist);
        document.getElementById('artistSearchInput').addEventListener('keypress', (e) => {
            if (e.key === 'Enter') searchArtist();
        });
        document.getElementById('addArtistBtn').addEventListener('click', addWatchedArtist);
        document.getElementById('refreshAllArtistsBtn').addEventListener('click', refreshAllArtists);

        // The three-way chooser. Cancel on any panel goes back to a clean slate
        // rather than to the previous panel; half-open forms are nobody's friend.
        document.querySelectorAll('#artistModeChooser [data-artist-mode]').forEach(btn => {
            btn.addEventListener('click', () => chooseArtistMode(btn.dataset.artistMode));
        });
        for (const id of ['cancelArtistBtn', 'cancelArtistChooserBtn',
                          'cancelArtistAlbumsBtn', 'cancelArtistOneOffBtn']) {
            document.getElementById(id)?.addEventListener('click', resetArtistChooser);
        }

        document.getElementById('artistAlbumBatchBtn')?.addEventListener('click', followArtistForAlbums);
        document.getElementById('artistAlbumSelectAllBtn')?.addEventListener('click', () => {
            document.querySelectorAll('#artistAlbumChecklist input[type="checkbox"]')
                .forEach(cb => { cb.checked = true; });
            updateAlbumChecklistCount();
        });
        document.getElementById('artistAlbumSelectNoneBtn')?.addEventListener('click', () => {
            document.querySelectorAll('#artistAlbumChecklist input[type="checkbox"]')
                .forEach(cb => { cb.checked = false; });
            updateAlbumChecklistCount();
        });

        // -----------------------------------------------------------------------
        // Per-artist album panel: one shared mount, re-targeted to whichever
        // followed artist's "Albums" button was last tapped. Backs onto the
        // shared album browser from the "Shared album browser" section above.
        // -----------------------------------------------------------------------

        let artistAlbumBrowserHandle = null;

        function openArtistAlbumPanel(artistId) {
            const artist = watchedArtistsById.get(artistId);
            const panel = document.getElementById('artistAlbumPanel');
            const heading = document.getElementById('artistAlbumPanelHeading');
            const autoAddToggle = document.getElementById('artistAutoAddToggle');
            const mountEl = document.getElementById('artistAlbumBrowserMount');
            if (!artist || !panel || !mountEl) return;

            panel.style.display = 'block';
            panel.dataset.artistId = artistId;
            if (heading) heading.textContent = `Albums by ${artist.name}`;
            // auto_add_albums may not exist yet on this deployment; default unchecked.
            if (autoAddToggle) autoAddToggle.checked = !!artist.auto_add_albums;

            loadArtistKnownAlbums(artistId);

            if (artistAlbumBrowserHandle) {
                artistAlbumBrowserHandle.setArtist(artist.mbid, artist.name);
            } else {
                artistAlbumBrowserHandle = mountAlbumBrowser(mountEl, {
                    artist: { mbid: artist.mbid, name: artist.name },
                    showArtistSearch: false,
                    onQueued: () => {
                        const pid = document.getElementById('artistAlbumPanel')?.dataset.artistId;
                        if (pid) loadArtistKnownAlbums(pid);
                    },
                });
            }

            panel.scrollIntoView({ behavior: 'smooth', block: 'start' });
        }

        async function loadArtistKnownAlbums(artistId) {
            const el = document.getElementById('artistKnownAlbums');
            if (!el) return;
            el.innerHTML = '<div class="loading"><div class="spinner"></div></div>';
            try {
                const res = await apiFetch(`/api/watched-artists/${artistId}/albums`);
                if (!res.ok) {
                    // Not landed on this deployment yet (or a genuine error) - degrade
                    // quietly rather than spamming the console, the browser below still works.
                    el.innerHTML = '<p style="font-size:12px;color:var(--text-secondary);padding:4px 0;">Album status tracking isn’t available yet.</p>';
                    return;
                }
                const data = await res.json();
                const albums = data.albums || [];
                if (!albums.length) {
                    el.innerHTML = '<p style="font-size:12px;color:var(--text-secondary);padding:4px 0;">No albums seen yet.</p>';
                    return;
                }
                const statusLabel = {
                    seen: 'Seen',
                    queued: 'Queued',
                    processing: 'Retrying',
                    downloading: 'Downloading',
                    cancelling: 'Cancelling',
                    complete: 'Complete',
                    on_disk: 'On disk',
                    incomplete: 'Incomplete',
                    cancelled: 'Cancelled',
                    failed: 'Failed',
                };
                el.innerHTML = albums.map(a => `
                    <div style="display:flex;align-items:center;gap:8px;padding:4px 0;font-size:12px;">
                        <span style="flex:1;">${escapeHtml(a.title)}${a.year ? ` <span style="color:var(--text-secondary);">(${escapeHtml(String(a.year))})</span>` : ''}</span>
                        ${a.display_status === 'complete' || a.display_status === 'on_disk'
                            ? `<i class="fa-solid fa-check" style="color:var(--success);" title="${escapeHtml(a.status_detail || 'On disk')}"></i>`
                            : a.display_status === 'incomplete'
                                ? `<i class="fa-solid fa-triangle-exclamation" style="color:var(--warning);" title="${escapeHtml(a.status_detail || 'Incomplete album')}"></i>`
                                : ''}
                        <span title="${escapeHtml(a.status_detail || '')}" style="color:${a.display_status === 'complete' || a.display_status === 'on_disk' ? 'var(--success)' : a.display_status === 'incomplete' || a.display_status === 'failed' ? 'var(--warning)' : 'var(--text-secondary)'};">${escapeHtml(statusLabel[a.display_status] || a.display_status || statusLabel[a.status] || a.status || '')}</span>
                    </div>
                `).join('');
            } catch (e) {
                el.innerHTML = '<p style="font-size:12px;color:var(--text-secondary);padding:4px 0;">Album status tracking isn’t available yet.</p>';
            }
        }

        document.getElementById('artistAlbumPanelCloseBtn')?.addEventListener('click', () => {
            const panel = document.getElementById('artistAlbumPanel');
            if (panel) panel.style.display = 'none';
        });

        document.getElementById('artistAutoAddToggle')?.addEventListener('change', async (e) => {
            const panel = document.getElementById('artistAlbumPanel');
            const artistId = panel?.dataset.artistId;
            if (!artistId) return;
            const checked = e.target.checked;
            try {
                await apiFetch(`/api/watched-artists/${artistId}`, {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ auto_add_albums: checked }),
                });
                showToast(checked
                    ? 'New albums will be added automatically from now on (the back catalogue is left alone)'
                    : 'Automatic album adding switched off');
            } catch (err) {
                showToast('Failed to update album setting', true);
                e.target.checked = !checked;
            }
        });

        // =============================================================================
        // Statistics Dashboard
        // =============================================================================

        let statsScanTimer = null;
        async function loadStats(refreshing = false) {
            clearTimeout(statsScanTimer);
            if (!refreshing) {
                statsContent.innerHTML = `
                    <div class="loading stats-loading" role="status">
                        <div class="stats-equaliser" aria-hidden="true">
                            <span></span><span></span><span></span>
                        </div>
                        <span>Crunching the numbers…</span>
                    </div>
                `;
            }

            try {
                const response = await apiFetch('/api/stats');
                if (!response.ok) throw new Error('Failed to load stats');

                const data = await response.json();
                renderStats(data);
                if (data.storage_scan_state === 'refreshing') {
                    statsScanTimer = setTimeout(() => {
                        if (statsTabContainer.classList.contains('tab-visible') && document.getElementById('app').style.display !== 'none') loadStats(true);
                    }, 3000);
                }
            } catch (error) {
                statsContent.innerHTML = `
                    <div class="empty-state">
                        <div class="empty-state-icon"><i class="fa-solid fa-circle-exclamation"></i></div>
                        <p>Failed to load statistics</p>
                    </div>
                `;
            }

            loadMismatches();
        }

        async function loadMismatches() {
            const section = document.getElementById('mismatchSection');
            const content = document.getElementById('mismatchContent');
            if (!section || !content) return;

            try {
                const res = await apiFetch('/api/mismatches');
                if (!res.ok) { section.style.display = 'none'; return; }
                const data = await res.json();
                const rows = data.mismatches || [];
                section.style.display = rows.length > 0 ? 'block' : 'none';
                if (rows.length === 0) return;

                content.innerHTML = rows.map(m => `
                    <div class="mismatch-row">
                        <div class="mismatch-meta">
                            <span class="mismatch-playlist">${escapeHtml(m.playlist_name || m.playlist_id || '—')}</span>
                            <span class="mismatch-date">${m.created_at ? m.created_at.slice(0, 16).replace('T', ' ') : ''}</span>
                        </div>
                        <div class="mismatch-pair">
                            <div class="mismatch-line mismatch-expected">
                                <span class="mismatch-tag">Expected</span>
                                <span>${escapeHtml(m.expected_artist)} &ndash; ${escapeHtml(m.expected_title)}</span>
                            </div>
                            <div class="mismatch-line mismatch-got">
                                <span class="mismatch-tag">Got</span>
                                <span>${escapeHtml(m.actual_artist || 'Unknown')} &ndash; ${escapeHtml(m.actual_title || 'Unknown')}</span>
                            </div>
                        </div>
                        <details class="mismatch-normalised">
                            <summary>Normalised</summary>
                            <div class="mismatch-norm-line"><span class="mismatch-tag">Exp</span> ${escapeHtml(m.exp_normalised)}</div>
                            <div class="mismatch-norm-line"><span class="mismatch-tag">Got</span> ${escapeHtml(m.got_normalised)}</div>
                        </details>
                        <button class="mismatch-accept-btn" data-mismatch-id="${m.id}" title="Re-download this track, ignoring the name mismatch">Force Download</button>
                    </div>
                `).join('');

                // Wire up force-accept buttons via delegation
                content.querySelectorAll('.mismatch-accept-btn').forEach(btn => {
                    btn.addEventListener('click', async () => {
                        const mid = btn.dataset.mismatchId;
                        btn.disabled = true;
                        btn.textContent = 'Queued...';
                        try {
                            const res = await apiFetch(`/api/mismatches/${mid}/accept`, { method: 'POST' });
                            if (res.status === 404) {
                                // Already dealt with elsewhere (e.g. Force Download from the
                                // Queue tab, which clears this same row as a side effect).
                                // Stale, not broken, so drop it quietly rather than crying wolf.
                                showToast('Already handled elsewhere');
                                btn.closest('.mismatch-row').remove();
                                if (!content.querySelector('.mismatch-row')) {
                                    section.style.display = 'none';
                                }
                                return;
                            }
                            if (!res.ok) {
                                const err = await res.json().catch(() => ({}));
                                throw new Error(err.detail || 'Failed');
                            }
                            showToast('Re-queued with mismatch check disabled');
                            btn.closest('.mismatch-row').remove();
                            // Hide the section if no rows remain
                            if (!content.querySelector('.mismatch-row')) {
                                section.style.display = 'none';
                            }
                        } catch (e) {
                            showToast(`Force download failed: ${e.message}`, true);
                            btn.disabled = false;
                            btn.textContent = 'Force Download';
                        }
                    });
                });
            } catch {
                section.style.display = 'none';
            }
        }

        const clearMismatchesBtn = document.getElementById('clearMismatchesBtn');
        if (clearMismatchesBtn) {
            clearMismatchesBtn.addEventListener('click', async () => {
                if (!confirm('Clear the entire mismatch log?')) return;
                await apiFetch('/api/mismatches', { method: 'DELETE' });
                loadMismatches();
            });
        }

        function formatBytes(bytes) {
            if (bytes === 0) return '0 B';
            const units = ['B', 'KB', 'MB', 'GB', 'TB'];
            const i = Math.floor(Math.log(bytes) / Math.log(1024));
            return (bytes / Math.pow(1024, i)).toFixed(i > 0 ? 1 : 0) + ' ' + units[i];
        }

        function renderStats(data) {
            const successRate = data.total_jobs > 0
                ? Math.round((data.completed / data.total_jobs) * 100)
                : 0;
            const searchSuccessRate = data.total_searches > 0
                ? Math.round((data.successful_searches / data.total_searches) * 100)
                : 0;
            const searchToDownloadRate = data.total_searches > 0
                ? Math.round((data.converted_searches / data.total_searches) * 100)
                : 0;

            // Build daily chart (simple bar chart using divs)
            const maxDaily = Math.max(...data.daily.map(d => d.count), 1);
            const dailyBars = data.daily.length > 0
                ? data.daily.slice(-14).map(d => {
                    const height = Math.max(4, Math.round((d.count / maxDaily) * 80));
                    const dayLabel = d.day;
                    return `
                        <div style="display: flex; flex-direction: column; align-items: center; gap: 4px; flex: 1; min-width: 0;">
                            <div style="font-size: 10px; color: var(--text-secondary);">${d.count}</div>
                            <div style="width: 100%; max-width: 24px; height: ${height}px; background: var(--accent); border-radius: 3px;"></div>
                            <div style="font-size: 9px; color: var(--text-secondary); white-space: nowrap;">${dayLabel}</div>
                        </div>
                    `;
                }).join('')
                : '<div style="text-align: center; color: var(--text-secondary); font-size: 13px; padding: 20px;">No downloads yet</div>';

            // Source breakdown
            const ytCount = data.sources.youtube || 0;
            const pxCount = data.sources.mp3phoenix || 0;
            const scCount = data.sources.soundcloud || 0;
            const zvCount = data.sources.zvu4no || 0;
            const fmcCount = data.sources.freemp3cloud || 0;
            const slkCount = data.sources.soulseek || 0;
            const monoCount = data.sources.monochrome || 0;
            const sourceTotal = ytCount + pxCount + scCount + zvCount + fmcCount + slkCount + monoCount || 1;

            let html = `
                <!-- Summary cards -->
                <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr)); gap: 12px; margin-bottom: 16px;">
                    <div style="padding: 14px; background: var(--bg-secondary); border: 1px solid var(--border); border-radius: 12px; text-align: center;" title="Total downloads that completed successfully">
                        <div style="font-size: 22px; font-weight: 600;">${data.completed}</div>
                        <div style="font-size: 11px; color: var(--text-secondary);">Completed</div>
                    </div>
                    <div style="padding: 14px; background: var(--bg-secondary); border: 1px solid var(--border); border-radius: 12px; text-align: center;" title="Downloads that failed  -  check the Queue tab for error details">
                        <div style="font-size: 22px; font-weight: 600; color: var(--error);">${data.failed}</div>
                        <div style="font-size: 11px; color: var(--text-secondary);">Failed</div>
                    </div>
                    <div style="padding: 14px; background: var(--bg-secondary); border: 1px solid var(--border); border-radius: 12px; text-align: center;" title="Percentage of all download attempts that completed successfully">
                        <div style="font-size: 22px; font-weight: 600;">${successRate}%</div>
                        <div style="font-size: 11px; color: var(--text-secondary);">Success rate</div>
                    </div>
                    <div style="padding: 14px; background: var(--bg-secondary); border: 1px solid var(--border); border-radius: 12px; text-align: center;" title="Number of audio files in your library and total disk space used">
                        <div style="font-size: 22px; font-weight: 600;">${data.file_count == null ? (data.storage_scan_state === 'error' ? 'Unavailable' : 'Scanning…') : data.file_count}</div>
                        <div style="font-size: 11px; color: var(--text-secondary);">${data.storage_bytes == null ? (data.storage_scan_state === 'error' ? 'Scan failed; reopen Stats to retry' : 'Reading library storage') : formatBytes(data.storage_bytes)}${data.storage_bytes != null && data.storage_scan_state === 'refreshing' ? ' (refreshing)' : data.storage_bytes != null && data.storage_scan_state === 'error' ? ' (refresh failed)' : ''}</div>
                    </div>
                    <div style="padding: 14px; background: var(--bg-secondary); border: 1px solid var(--border); border-radius: 12px; text-align: center;" title="${searchSuccessRate}% of searches returned at least one result">
                        <div style="font-size: 22px; font-weight: 600;">${data.total_searches || 0}</div>
                        <div style="font-size: 11px; color: var(--text-secondary);">Searches</div>
                    </div>
                    <div style="padding: 14px; background: var(--bg-secondary); border: 1px solid var(--border); border-radius: 12px; text-align: center;" title="Percentage of searches that ended with you actually downloading something">
                        <div style="font-size: 22px; font-weight: 600;">${searchToDownloadRate}%</div>
                        <div style="font-size: 11px; color: var(--text-secondary);">Search → Download</div>
                    </div>
                </div>

                <!-- Search quality -->
                <div style="padding: 16px; background: var(--bg-secondary); border: 1px solid var(--border); border-radius: 12px; margin-bottom: 16px;">
                    <div style="font-size: 13px; font-weight: 600; color: var(--text-secondary); margin-bottom: 8px;">Search Performance</div>
                    <div style="font-size: 13px; color: var(--text-secondary);">
                        Successful searches: <strong style="color: var(--text-primary);">${data.successful_searches || 0}</strong> /
                        ${data.total_searches || 0}
                        (${searchSuccessRate}% found at least one result)
                    </div>
                </div>

                <!-- Daily downloads chart -->
                <div style="padding: 16px; background: var(--bg-secondary); border: 1px solid var(--border); border-radius: 12px; margin-bottom: 16px;">
                    <div style="font-size: 13px; font-weight: 600; color: var(--text-secondary); margin-bottom: 12px;">Downloads (last 14 days)</div>
                    <div style="display: flex; align-items: flex-end; gap: 4px; min-height: 100px;">
                        ${dailyBars}
                    </div>
                </div>

                <!-- Source breakdown -->
                <div style="padding: 16px; background: var(--bg-secondary); border: 1px solid var(--border); border-radius: 12px; margin-bottom: 16px;">
                    <div style="font-size: 13px; font-weight: 600; color: var(--text-secondary); margin-bottom: 12px;">Sources</div>
                    <div style="display: flex; gap: 8px; height: 8px; border-radius: 4px; overflow: hidden; margin-bottom: 8px;">
                        ${ytCount > 0 ? `<div style="flex: ${ytCount}; background: #ff0000; border-radius: 4px;"></div>` : ''}
                        ${pxCount > 0 ? `<div style="flex: ${pxCount}; background: #e05c00; border-radius: 4px;"></div>` : ''}
                        ${scCount > 0 ? `<div style="flex: ${scCount}; background: #ff5500; border-radius: 4px;"></div>` : ''}
                        ${zvCount > 0 ? `<div style="flex: ${zvCount}; background: #7a6aee; border-radius: 4px;"></div>` : ''}
                        ${fmcCount > 0 ? `<div style="flex: ${fmcCount}; background: #3a2fd6; border-radius: 4px;"></div>` : ''}
                        ${monoCount > 0 ? `<div style="flex: ${monoCount}; background: #0f766e; border-radius: 4px;"></div>` : ''}
                        ${slkCount > 0 ? `<div style="flex: ${slkCount}; background: #4a9eff; border-radius: 4px;"></div>` : ''}
                    </div>
                    <div style="display: flex; gap: 16px; font-size: 12px; flex-wrap: wrap;">
                        <span style="color: var(--text-secondary);"><span style="display: inline-block; width: 8px; height: 8px; background: #ff0000; border-radius: 2px; margin-right: 4px;"></span>YouTube: ${ytCount}</span>
                        ${pxCount > 0 ? `<span style="color: var(--text-secondary);"><span style="display: inline-block; width: 8px; height: 8px; background: #e05c00; border-radius: 2px; margin-right: 4px;"></span>MP3Phoenix: ${pxCount}</span>` : ''}
                        ${scCount > 0 ? `<span style="color: var(--text-secondary);"><span style="display: inline-block; width: 8px; height: 8px; background: #ff5500; border-radius: 2px; margin-right: 4px;"></span>SoundCloud: ${scCount}</span>` : ''}
                        ${zvCount > 0 ? `<span style="color: var(--text-secondary);"><span style="display: inline-block; width: 8px; height: 8px; background: #7a6aee; border-radius: 2px; margin-right: 4px;"></span>zvu4no: ${zvCount}</span>` : ''}
                        ${fmcCount > 0 ? `<span style="color: var(--text-secondary);"><span style="display: inline-block; width: 8px; height: 8px; background: #3a2fd6; border-radius: 2px; margin-right: 4px;"></span>FreeMp3Cloud: ${fmcCount}</span>` : ''}
                        ${monoCount > 0 ? `<span style="color: var(--text-secondary);"><span style="display: inline-block; width: 8px; height: 8px; background: #0f766e; border-radius: 2px; margin-right: 4px;"></span>Monochrome: ${monoCount}</span>` : ''}
                        <span style="color: var(--text-secondary);"><span style="display: inline-block; width: 8px; height: 8px; background: #4a9eff; border-radius: 2px; margin-right: 4px;"></span>Soulseek: ${slkCount}</span>
                    </div>
                </div>
            `;

            // Top artists
            if (data.top_artists.length > 0) {
                const maxArtistCount = data.top_artists[0].count;
                html += `
                    <div style="padding: 16px; background: var(--bg-secondary); border: 1px solid var(--border); border-radius: 12px; margin-bottom: 16px;">
                        <div style="font-size: 13px; font-weight: 600; color: var(--text-secondary); margin-bottom: 12px;">Top Artists</div>
                        ${data.top_artists.map((a, i) => `
                            <div style="display: flex; align-items: center; gap: 10px; margin-bottom: 8px;">
                                <span style="font-size: 11px; color: var(--text-secondary); width: 16px; text-align: right;">${i + 1}</span>
                                <div style="flex: 1; min-width: 0;">
                                    <div style="display: flex; align-items: center; gap: 8px;">
                                        <span style="font-size: 13px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;">${escapeHtml(a.artist)}</span>
                                        <span style="font-size: 11px; color: var(--text-secondary); flex-shrink: 0;">${a.count}</span>
                                    </div>
                                    <div style="height: 4px; background: var(--bg-tertiary); border-radius: 2px; margin-top: 4px;">
                                        <div style="height: 100%; width: ${Math.round((a.count / maxArtistCount) * 100)}%; background: var(--accent); border-radius: 2px;"></div>
                                    </div>
                                </div>
                            </div>
                        `).join('')}
                    </div>
                `;
            }

            // Top searched artists
            if (data.top_searched_artists && data.top_searched_artists.length > 0) {
                const maxSearchCount = data.top_searched_artists[0].count;
                html += `
                    <div style="padding: 16px; background: var(--bg-secondary); border: 1px solid var(--border); border-radius: 12px; margin-bottom: 16px;">
                        <div style="font-size: 13px; font-weight: 600; color: var(--text-secondary); margin-bottom: 12px;">Most Searched Artists</div>
                        ${data.top_searched_artists.map((a, i) => `
                            <div style="display: flex; align-items: center; gap: 10px; margin-bottom: 8px;">
                                <span style="font-size: 11px; color: var(--text-secondary); width: 16px; text-align: right;">${i + 1}</span>
                                <div style="flex: 1; min-width: 0;">
                                    <div style="display: flex; align-items: center; gap: 8px;">
                                        <span style="font-size: 13px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;">${escapeHtml(a.artist)}</span>
                                        <span style="font-size: 11px; color: var(--text-secondary); flex-shrink: 0;">${a.count}</span>
                                    </div>
                                    <div style="height: 4px; background: var(--bg-tertiary); border-radius: 2px; margin-top: 4px;">
                                        <div style="height: 100%; width: ${Math.round((a.count / maxSearchCount) * 100)}%; background: #f59e0b; border-radius: 2px;"></div>
                                    </div>
                                </div>
                            </div>
                        `).join('')}
                    </div>
                `;
            }

            // Recent downloads
            if (data.recent.length > 0) {
                html += `
                    <div style="padding: 16px; background: var(--bg-secondary); border: 1px solid var(--border); border-radius: 12px;">
                        <div style="font-size: 13px; font-weight: 600; color: var(--text-secondary); margin-bottom: 12px;">Recent Downloads</div>
                        ${data.recent.map(r => `
                            <div style="display: flex; align-items: center; gap: 8px; margin-bottom: 6px; font-size: 12px;">
                                <span class="source-badge ${escapeHtml(r.source || 'youtube')}" style="flex-shrink: 0;">${getSourceBadge(r.source || 'youtube')}</span>
                                <span style="white-space: nowrap; overflow: hidden; text-overflow: ellipsis;">${escapeHtml(r.artist ? `${r.artist} - ${r.title}` : r.title)}</span>
                                <span style="margin-left: auto; color: var(--text-secondary); white-space: nowrap; flex-shrink: 0;">${r.completed_at ? formatTime(r.completed_at) : ''}</span>
                            </div>
                        `).join('')}
                    </div>
                `;
            }

            statsContent.innerHTML = `<div class="stats-reveal">${html}</div>`;
        }

        // =============================================================================
        // Settings Management
        // =============================================================================

        // Mapping from setting keys to form element IDs
        const settingsFieldMap = {
            'skip_dupes': 'settingSkipDupes',
            'enable_musicbrainz': 'settingEnableMusicbrainz',
            'enable_deezer_metadata': 'settingEnableDeezerMetadata',
            'enable_lyrics': 'settingEnableLyrics',
            'acoustid_api_key': 'settingAcoustidKey',
            'default_convert_audio': 'settingDefaultConvertAudio',
            'audio_format': 'settingAudioFormat',
            'mp3_bitrate': 'settingMp3Bitrate',
            'opus_bitrate': 'settingOpusBitrate',
            'alac_bitrate': 'settingAlacBitrate',
            'convert_lossless_only': 'settingConvertLosslessOnly',
            'normalise_lossy_audio': 'settingNormaliseLossyAudio',
            'enable_replaygain': 'settingEnableReplaygain',
            'replaygain_replace_existing': 'settingReplaygainReplaceExisting',
            'min_audio_bitrate': 'settingMinBitrate',
            'reject_live_versions': 'settingRejectLiveVersions',
            'reject_version_keywords': 'settingRejectVersionKeywords',
            'playlist_comment_tagging': 'settingPlaylistCommentTagging',
            'redownload_missing_files': 'settingRedownloadMissingFiles',
            'enable_track_upgrades': 'settingEnableTrackUpgrades',
            'upgrade_scan_interval_hours': 'settingUpgradeScanInterval',
            'singles_subdir': 'settingSinglesSubdir',
            'playlists_subdir': 'settingPlaylistsSubdir',
            'albums_subdir': 'settingAlbumsSubdir',
            'auto_import_dir': 'settingAutoImportDir',
            'organise_by_artist': 'settingOrganiseByArtist',
            'include_track_number_in_filename': 'settingIncludeTrackNumberInFilename',
            'auto_album_singles': 'settingAutoAlbumSingles',
            'auto_album_singles_use_albums_dir': 'settingAutoAlbumSinglesUseAlbumsDir',
            'playlist_album_as_name': 'settingPlaylistAlbumAsName',
            'singles_only_mode': 'settingSinglesOnlyMode',
            'source_youtube_enabled': 'settingSourceYoutube',
            'source_soundcloud_enabled': 'settingSourceSoundcloud',
            'source_zvu4no_enabled': 'settingSourceZvu4no',
            'source_freemp3cloud_enabled': 'settingSourceFreemp3cloud',
            'source_soulseek_enabled': 'settingSourceSoulseek',
            'source_monochrome_enabled': 'settingSourceMonochrome',
            'source_offline_fallback': 'settingSourceOfflineFallback',
            'youtube_requested_video_fallback': 'settingYoutubeRequestedVideoFallback',
            'source_health_checks_enabled': 'settingSourceHealthChecks',
            'source_health_check_interval_minutes': 'settingSourceHealthInterval',
            'source_health_cooldown_minutes': 'settingSourceHealthCooldown',
            'search_concurrency': 'settingSearchConcurrency',
            'slskd_url': 'settingSlskdUrl',
            'slskd_user': 'settingSlskdUser',
            'slskd_pass': 'settingSlskdPass',
            'slskd_downloads_path': 'settingSlskdDownloads',
            'slskd_move_completed': 'settingSlskdMoveCompleted',
            'navidrome_url': 'settingNavidromeUrl',
            'navidrome_user': 'settingNavidromeUser',
            'navidrome_pass': 'settingNavidromePass',
            'jellyfin_url': 'settingJellyfinUrl',
            'jellyfin_api_key': 'settingJellyfinApiKey',
            'plex_url': 'settingPlexUrl',
            'plex_token': 'settingPlexToken',
            'lidarr_url': 'settingLidarrUrl',
            'lidarr_api_key': 'settingLidarrApiKey',
            'notify_on': 'settingNotifyOn',
            'telegram_webhook_url': 'settingTelegramUrl',
            'webhook_url': 'settingWebhookUrl',
            'apprise_url': 'settingAppriseUrl',
            'smtp_host': 'settingSmtpHost',
            'smtp_port': 'settingSmtpPort',
            'smtp_user': 'settingSmtpUser',
            'smtp_pass': 'settingSmtpPass',
            'smtp_from': 'settingSmtpFrom',
            'smtp_to': 'settingSmtpTo',
            'smtp_tls': 'settingSmtpTls',
            'youtube_cookies': 'settingYoutubeCookies',
            'spotify_cookies': 'settingSpotifyCookies',
            'apple_music_user_token': 'settingAppleMusicUserToken',
            'spotify_browser_timeout_seconds': 'settingSpotifyBrowserTimeout',
            'spotify_browser_stall_seconds': 'settingSpotifyBrowserStall',
            'file_permissions': 'settingFilePermissions',
            'api_key': 'settingApiKey'
        };

        // Track which fields are locked by env vars
        let envOverrides = [];
        let sensitiveFields = [];
        let settingsLoaded = false;
        let originalValues = {}; // Track original values to detect changes

        async function loadSettings() {
            if (settingsLoaded) return; // Only load once per session

            try {
                const response = await apiFetch('/api/settings');
                if (!response.ok) throw new Error('Failed to load settings');

                const data = await response.json();
                const settings = data.settings;
                envOverrides = data.env_overrides || [];
                sensitiveFields = data.sensitive_fields || [];

                // Populate form fields and track original values
                for (const [key, elementId] of Object.entries(settingsFieldMap)) {
                    const element = document.getElementById(elementId);
                    if (!element) continue;

                    const value = settings[key];
                    const isLocked = envOverrides.includes(key);

                    // Store original value for change detection
                    if (!element.dataset.defaultPlaceholder) {
                        element.dataset.defaultPlaceholder = element.placeholder || '';
                    }

                    if (element.type === 'checkbox') {
                        originalValues[key] = value === true || value === 'true';
                    } else if (sensitiveFields.includes(key) && value === '••••••••') {
                        originalValues[key] = null; // Indicates "configured but hidden"
                    } else {
                        originalValues[key] = value || '';
                    }

                    if (element.type === 'checkbox') {
                        element.checked = value === true || value === 'true';
                    } else {
                        // For sensitive fields, only show placeholder if value is masked
                        if (sensitiveFields.includes(key) && value === '••••••••') {
                            element.placeholder = 'Configured (hidden)';
                            element.value = '';
                        } else {
                            element.value = value || '';
                        }
                        // Sync format and quality picker buttons when their settings load
                        if (key === 'audio_format') setAudioFormat(value);
                        if (key === 'mp3_bitrate')  setMp3Bitrate(value);
                        if (key === 'opus_bitrate') setOpusBitrate(value);
                        if (key === 'alac_bitrate') setAlacBitrate(value);
                    }

                    // Mark fields locked by env vars
                    if (isLocked) {
                        element.disabled = true;
                        const row = element.closest('.settings-row') || element.closest('.settings-label');
                        if (row) {
                            row.classList.add('env-locked');
                            row.title = 'Set via environment variable (e.g., docker-compose.yml)';
                        }
                    }
                }

                // The conversion-mode buttons are the visible control for the
                // hidden persisted checkbox.
                setDefaultConversionMode(document.getElementById('settingDefaultConvertAudio').checked);

                settingsLoaded = true;

                // Sync notify_on checkboxes from the hidden field value
                _syncNotifyOnCheckboxes();

                // Populate singles subfolder dropdown from actual directories
                await _populateSubdirDropdown(settings['singles_subdir'] || 'Singles');
                // Populate playlists subfolder dropdown
                await _populatePlaylistsSubdirDropdown(settings['playlists_subdir'] || '');
                // Populate albums subfolder dropdown
                await _populateAlbumsSubdirDropdown(settings['albums_subdir'] || 'Albums');

                // Live path preview: update whenever filename layout toggles change
                const organiseToggle = document.getElementById('settingOrganiseByArtist');
                if (organiseToggle) organiseToggle.onchange = _updatePathPreviews;
                const trackNumberToggle = document.getElementById('settingIncludeTrackNumberInFilename');
                if (trackNumberToggle) trackNumberToggle.onchange = _updatePathPreviews;

                // Grey out "Route to Albums folder" when auto-album routing is off
                function _updateAlbumsDirToggle() {
                    const routeRow = document.getElementById('autoAlbumSinglesUseAlbumsDirRow');
                    const routeInput = document.getElementById('settingAutoAlbumSinglesUseAlbumsDir');
                    const autoAlbumOn = document.getElementById('settingAutoAlbumSingles')?.checked;
                    if (routeRow) routeRow.style.opacity = autoAlbumOn ? '' : '0.4';
                    if (routeInput) routeInput.disabled = !autoAlbumOn;
                }
                const autoAlbumToggle = document.getElementById('settingAutoAlbumSingles');
                if (autoAlbumToggle) {
                    autoAlbumToggle.addEventListener('change', _updateAlbumsDirToggle);
                    _updateAlbumsDirToggle();
                }

                // Update browser API key status
                updateBrowserApiKeyStatus();

                // Inject clear buttons into settings rows
                _injectSettingsClearButtons();

                // Show cookie expiry status if cookies are configured
                _updateCookieExpiryHint();
                _updateSpotifyCookieHint(settings);
                await loadMaintenancePreview();
            } catch (error) {
                console.error('Failed to load settings:', error);
                showToast('Failed to load settings', true);
            }
        }

        async function loadMaintenancePreview() {
            const preview = document.getElementById('maintenancePreview');
            if (!preview || !isAdmin()) return;
            try {
                const response = await apiFetch('/api/settings/maintenance');
                if (!response.ok) throw new Error('Preview failed');
                const data = await response.json();
                const history = data.queue_history || {};
                preview.textContent = `${data.orphan_imports || 0} orphan import(s); `
                    + `${history.completed || 0} completed, ${history.completed_with_errors || 0} completed with issues, `
                    + `${history.failed || 0} failed, ${history.cancelled || 0} cancelled Queue row(s); `
                    + `${data.indexed_paths || 0} exact library path(s) indexed.`;
            } catch (error) {
                preview.textContent = 'Could not load the maintenance preview.';
            }
        }

        async function _postMaintenance(payload, confirmation) {
            if (!confirm(confirmation + ' Library audio will not be deleted or moved.')) return;
            const result = document.getElementById('maintenanceResult');
            if (result) result.textContent = 'Working...';
            try {
                const response = await apiFetch('/api/settings/maintenance', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ ...payload, confirm: true })
                });
                const data = await response.json();
                if (!response.ok) throw new Error(data.detail || 'Maintenance failed');
                if (result) result.textContent = Object.entries(data)
                    .map(([key, value]) => `${key.replaceAll('_', ' ')}: ${value}`)
                    .join('; ');
                showToast('Maintenance complete');
                await loadMaintenancePreview();
                loadJobs();
            } catch (error) {
                if (result) {
                    result.textContent = error.message || 'Maintenance failed';
                    result.classList.add('error');
                }
                showToast(error.message || 'Maintenance failed', true);
            }
        }

        function runMaintenanceAction(action) {
            const labels = {
                purge_orphans: 'Remove imports whose watched playlist or artist no longer exists?',
                reconcile: 'Check indexed files and mark missing ones as deleted in Queue history?',
                clean_rebuild: 'Remove orphan imports, reconcile missing files, and index untracked audio beneath configured library folders?'
            };
            return _postMaintenance({ action }, labels[action] || 'Run maintenance?');
        }

        function clearSelectedQueueHistory() {
            const statuses = Array.from(document.querySelectorAll('.maintenance-history input:checked'))
                .map(input => input.value);
            if (!statuses.length) {
                showToast('Select at least one Queue history type', true);
                return;
            }
            return _postMaintenance(
                { action: 'clear_history', statuses },
                `Permanently remove ${statuses.length} selected Queue history type(s) from the database?`
            );
        }

        const SUBDIR_CUSTOM_VALUE = '__custom__';

        function _normaliseSubdirPath(rawValue) {
            const raw = String(rawValue || '').trim();
            if (!raw) return '';
            if (raw === '.') return '.';

            const parts = raw
                .replace(/\\/g, '/')
                .split('/')
                .map(part => part.trim())
                .filter(part => part && part !== '.');

            if (parts.some(part => part === '..')) return '';
            return parts.join('/');
        }

        async function _populateSubdirDropdown(currentValue) {
            const select = document.getElementById('settingSinglesSubdir');
            const customRow = document.getElementById('customSubdirRow');
            const customInput = document.getElementById('customSubdirInput');
            if (!select || !customRow || !customInput) return;

            const currentRaw = (currentValue || 'Singles').trim();
            const current = currentRaw === '.' ? '.' : (_normaliseSubdirPath(currentRaw) || 'Singles');

            let dirs = [];
            try {
                const resp = await apiFetch('/api/music-dirs?recursive=true&max_depth=2');
                if (resp.ok) {
                    const data = await resp.json();
                    dirs = Array.isArray(data.directories) ? data.directories : [];
                }
            } catch (e) {
                console.warn('Could not fetch music directories:', e);
            }

            const unique = new Set();
            for (const dir of dirs) {
                const normalised = _normaliseSubdirPath(dir);
                if (normalised && normalised !== '.') {
                    unique.add(normalised);
                }
            }

            // Keep defaults/current value available even if folder disappeared.
            unique.add('Singles');
            if (current !== '.') {
                unique.add(current);
            }

            const sortedDirs = Array.from(unique).sort((a, b) =>
                a.localeCompare(b, undefined, { sensitivity: 'base' })
            );

            select.innerHTML = '';

            const musicRoot = (serverConfig && serverConfig.music_dir) ? serverConfig.music_dir.replace(/\/+$/, '') : '/music';

            const rootOpt = document.createElement('option');
            rootOpt.value = '.';
            rootOpt.textContent = `${musicRoot} (root)`;
            select.appendChild(rootOpt);

            for (const relPath of sortedDirs) {
                const opt = document.createElement('option');
                opt.value = relPath;
                opt.textContent = `${musicRoot}/${relPath}`;
                select.appendChild(opt);
            }

            const customOpt = document.createElement('option');
            customOpt.value = SUBDIR_CUSTOM_VALUE;
            customOpt.textContent = 'Custom path\u2026';
            select.appendChild(customOpt);

            const currentIsKnown = current === '.' || sortedDirs.includes(current);
            customInput.disabled = !!select.disabled;
            if (currentIsKnown) {
                select.value = current;
                customRow.style.display = 'none';
                customInput.value = '';
            } else {
                select.value = SUBDIR_CUSTOM_VALUE;
                customRow.style.display = 'block';
                customInput.value = current;
            }

            select.onchange = () => {
                if (select.value === SUBDIR_CUSTOM_VALUE) {
                    customRow.style.display = 'block';
                    customInput.focus();
                    _updatePathPreviews();
                    return;
                }
                customRow.style.display = 'none';
                customInput.value = '';
                _updatePathPreviews();
            };
            customInput.oninput = _updatePathPreviews;
            _updatePathPreviews();
        }

        async function _populatePlaylistsSubdirDropdown(currentValue) {
            const select = document.getElementById('settingPlaylistsSubdir');
            const customRow = document.getElementById('customPlaylistsSubdirRow');
            const customInput = document.getElementById('customPlaylistsSubdirInput');
            if (!select || !customRow || !customInput) return;

            const currentRaw = String(currentValue || '').trim();
            const current = currentRaw === '.' ? '.' : (_normaliseSubdirPath(currentRaw) || '');

            let dirs = [];
            try {
                const resp = await apiFetch('/api/music-dirs?recursive=true&max_depth=2');
                if (resp.ok) {
                    const data = await resp.json();
                    dirs = Array.isArray(data.directories) ? data.directories : [];
                }
            } catch (e) {
                console.warn('Could not fetch music directories:', e);
            }

            const unique = new Set();
            for (const dir of dirs) {
                const normalised = _normaliseSubdirPath(dir);
                if (normalised && normalised !== '.') {
                    unique.add(normalised);
                }
            }

            // Always include "Playlists" as a sensible default option
            unique.add('Playlists');
            if (current && current !== '.') {
                unique.add(current);
            }

            const sortedDirs = Array.from(unique).sort((a, b) =>
                a.localeCompare(b, undefined, { sensitivity: 'base' })
            );

            select.innerHTML = '';

            // First option: disabled (feature off)
            const disabledOpt = document.createElement('option');
            disabledOpt.value = '';
            disabledOpt.textContent = '(disabled)';
            select.appendChild(disabledOpt);

            const musicRoot = (serverConfig && serverConfig.music_dir) ? serverConfig.music_dir.replace(/\/+$/, '') : '/music';

            // Second option: musicRoot/Playlists as the obvious default
            const playlistsOpt = document.createElement('option');
            playlistsOpt.value = 'Playlists';
            playlistsOpt.textContent = `${musicRoot}/Playlists`;
            select.appendChild(playlistsOpt);

            const rootOpt = document.createElement('option');
            rootOpt.value = '.';
            rootOpt.textContent = `${musicRoot} (root)`;
            select.appendChild(rootOpt);

            for (const relPath of sortedDirs.filter(d => d !== 'Playlists')) {
                const opt = document.createElement('option');
                opt.value = relPath;
                opt.textContent = `${musicRoot}/${relPath}`;
                select.appendChild(opt);
            }

            const customOpt = document.createElement('option');
            customOpt.value = SUBDIR_CUSTOM_VALUE;
            customOpt.textContent = 'Custom path\u2026';
            select.appendChild(customOpt);

            const currentIsKnown = !current || current === '.' || sortedDirs.includes(current);
            customInput.disabled = !!select.disabled;
            if (currentIsKnown) {
                select.value = current;
                customRow.style.display = 'none';
                customInput.value = '';
            } else {
                select.value = SUBDIR_CUSTOM_VALUE;
                customRow.style.display = 'block';
                customInput.value = current;
            }

            select.onchange = () => {
                if (select.value === SUBDIR_CUSTOM_VALUE) {
                    customRow.style.display = 'block';
                    customInput.focus();
                    _updatePathPreviews();
                    return;
                }
                customRow.style.display = 'none';
                customInput.value = '';
                _updatePathPreviews();
            };
            customInput.oninput = _updatePathPreviews;
            _updatePathPreviews();
        }

        async function _populateAlbumsSubdirDropdown(currentValue) {
            const select = document.getElementById('settingAlbumsSubdir');
            const customRow = document.getElementById('customAlbumsSubdirRow');
            const customInput = document.getElementById('customAlbumsSubdirInput');
            if (!select || !customRow || !customInput) return;

            const currentRaw = String(currentValue || 'Albums').trim();
            const current = currentRaw === '.' ? '.' : (_normaliseSubdirPath(currentRaw) || 'Albums');

            let dirs = [];
            try {
                const resp = await apiFetch('/api/music-dirs?recursive=true&max_depth=2');
                if (resp.ok) {
                    const data = await resp.json();
                    dirs = Array.isArray(data.directories) ? data.directories : [];
                }
            } catch (e) {
                console.warn('Could not fetch music directories:', e);
            }

            const unique = new Set();
            for (const dir of dirs) {
                const normalised = _normaliseSubdirPath(dir);
                if (normalised && normalised !== '.') unique.add(normalised);
            }
            unique.add('Albums');
            if (current && current !== '.') unique.add(current);

            const sortedDirs = Array.from(unique).sort((a, b) =>
                a.localeCompare(b, undefined, { sensitivity: 'base' })
            );

            select.innerHTML = '';

            const musicRoot = (serverConfig && serverConfig.music_dir) ? serverConfig.music_dir.replace(/\/+$/, '') : '/music';

            const rootOpt = document.createElement('option');
            rootOpt.value = '.';
            rootOpt.textContent = `${musicRoot} (root)`;
            select.appendChild(rootOpt);

            for (const relPath of sortedDirs) {
                const opt = document.createElement('option');
                opt.value = relPath;
                opt.textContent = `${musicRoot}/${relPath}`;
                select.appendChild(opt);
            }

            const customOpt = document.createElement('option');
            customOpt.value = SUBDIR_CUSTOM_VALUE;
            customOpt.textContent = 'Custom path\u2026';
            select.appendChild(customOpt);

            const currentIsKnown = current === '.' || sortedDirs.includes(current);
            customInput.disabled = !!select.disabled;
            if (currentIsKnown) {
                select.value = current;
                customRow.style.display = 'none';
                customInput.value = '';
            } else {
                select.value = SUBDIR_CUSTOM_VALUE;
                customRow.style.display = 'block';
                customInput.value = current;
            }

            select.onchange = () => {
                if (select.value === SUBDIR_CUSTOM_VALUE) {
                    customRow.style.display = 'block';
                    customInput.focus();
                    _updatePathPreviews();
                    return;
                }
                customRow.style.display = 'none';
                customInput.value = '';
                _updatePathPreviews();
            };
            customInput.oninput = _updatePathPreviews;
            _updatePathPreviews();
        }

        async function _updateCookieExpiryHint() {
            const hint = document.getElementById('cookieExpiryHint');
            if (!hint) return;
            try {
                const resp = await apiFetch('/api/settings/youtube-cookies/status');
                if (!resp.ok) return;
                const data = await resp.json();
                if (!data.has_setting || !data.auth_cookie_expiry) {
                    hint.style.display = 'none';
                    return;
                }
                const expiryMs = data.auth_cookie_expiry * 1000;
                const now = Date.now();
                const daysLeft = Math.floor((expiryMs - now) / 86400000);
                const expiryDate = new Date(expiryMs).toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: 'numeric' });

                hint.style.display = 'block';
                if (now > expiryMs) {
                    hint.style.color = 'var(--error-color, #e53e3e)';
                    hint.textContent = `Cookies expired on ${expiryDate} - re-export them`;
                } else if (daysLeft <= 7) {
                    hint.style.color = 'var(--warning-color, #d97706)';
                    hint.textContent = `Cookies expire in ${daysLeft} day${daysLeft !== 1 ? 's' : ''} (${expiryDate}) - consider re-exporting soon`;
                } else {
                    hint.style.color = 'var(--text-secondary)';
                    hint.textContent = `Cookies valid until ${expiryDate}`;
                }
            } catch (e) {
                // Status fetch failing is not the end of the world
            }
        }

        function _updateSpotifyCookieHint(settings) {
            const banner = document.getElementById('spotifyCookiesExpiredBanner');
            const hint = document.getElementById('spotifyCookieExpiryHint');

            // Show expired banner when the flag is set and cookies are still present
            if (banner) {
                const expired = settings && settings['spotify_cookies_expired'] === true;
                const hasCookies = settings && settings['spotify_cookies'];
                banner.style.display = (expired && hasCookies) ? 'block' : 'none';
            }

            // Expiry hint: parse sp_dc expiry from the Netscape cookie text
            if (!hint) return;
            const cookiesText = settings && settings['spotify_cookies'];
            if (!cookiesText) {
                hint.style.display = 'none';
                return;
            }
            // Find sp_dc expiry from cookie lines
            let spDcExpiry = null;
            for (const line of cookiesText.split('\n')) {
                const l = line.trim();
                if (!l || l.startsWith('#')) continue;
                const parts = l.split('\t');
                if (parts.length >= 7 && parts[5] === 'sp_dc') {
                    const exp = parseInt(parts[4], 10);
                    if (exp > 0) spDcExpiry = exp;
                    break;
                }
            }
            if (!spDcExpiry) {
                hint.style.display = 'none';
                return;
            }
            const now = Date.now();
            const expiryMs = spDcExpiry * 1000;
            const daysLeft = Math.floor((expiryMs - now) / 86400000);
            const expiryDate = new Date(expiryMs).toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: 'numeric' });
            hint.style.display = 'block';
            if (now > expiryMs) {
                hint.style.color = 'var(--error-color, #e53e3e)';
                hint.textContent = `sp_dc expired on ${expiryDate} - re-export cookies`;
            } else if (daysLeft <= 30) {
                hint.style.color = 'var(--warning-color, #d97706)';
                hint.textContent = `sp_dc expires in ${daysLeft} day${daysLeft !== 1 ? 's' : ''} (${expiryDate})`;
            } else {
                hint.style.color = 'var(--text-secondary)';
                hint.textContent = `sp_dc valid until ${expiryDate}`;
            }
        }

        function _updatePathPreviews() {
            const singlesEl = document.getElementById('singlesPathPreview');
            const playlistsEl = document.getElementById('playlistsPathPreview');
            const albumsEl = document.getElementById('albumsPathPreview');
            if (!singlesEl && !playlistsEl && !albumsEl) return;

            const singlesSelect = document.getElementById('settingSinglesSubdir');
            const singlesCustom = document.getElementById('customSubdirInput');
            const playlistsSelect = document.getElementById('settingPlaylistsSubdir');
            const playlistsCustom = document.getElementById('customPlaylistsSubdirInput');
            const albumsSelect = document.getElementById('settingAlbumsSubdir');
            const albumsCustom = document.getElementById('customAlbumsSubdirInput');
            const organiseToggle = document.getElementById('settingOrganiseByArtist');
            const trackNumberToggle = document.getElementById('settingIncludeTrackNumberInFilename');

            const singlesVal = singlesSelect && singlesSelect.value === SUBDIR_CUSTOM_VALUE
                ? (singlesCustom ? singlesCustom.value.trim() : '')
                : (singlesSelect ? singlesSelect.value : '');
            const playlistsVal = playlistsSelect && playlistsSelect.value === SUBDIR_CUSTOM_VALUE
                ? (playlistsCustom ? playlistsCustom.value.trim() : '')
                : (playlistsSelect ? playlistsSelect.value : '');
            const organise = organiseToggle ? organiseToggle.checked : true;
            const includeTrackNumber = trackNumberToggle ? trackNumberToggle.checked : false;
            const titleExample = includeTrackNumber ? '1 - Track Title.flac' : 'Track Title.flac';
            const flatTitleExample = includeTrackNumber ? 'Artist Name - 1 - Track Title.flac' : 'Artist Name - Track Title.flac';

            const musicRoot = (serverConfig && serverConfig.music_dir) ? serverConfig.music_dir.replace(/\/+$/, '') : '/music';

            if (singlesEl) {
                let p = musicRoot;
                if (singlesVal && singlesVal !== '.') p += '/' + singlesVal;
                if (organise) {
                    p += '/Artist Name/' + titleExample;
                } else {
                    p += '/' + flatTitleExample;
                }
                singlesEl.textContent = 'Files saved to: ' + p;
            }

            if (playlistsEl) {
                if (!playlistsVal || playlistsVal === '') {
                    playlistsEl.textContent = '(playlist tracks go to Singles folder)';
                } else {
                    let p = musicRoot;
                    if (playlistsVal !== '.') p += '/' + playlistsVal;
                    p += '/Playlist Name/' + (includeTrackNumber ? 'Artist - 1 - Title.flac' : 'Artist - Title.flac');
                    playlistsEl.textContent = 'Files saved to: ' + p;
                }
            }

            const albumsVal = albumsSelect && albumsSelect.value === SUBDIR_CUSTOM_VALUE
                ? (albumsCustom ? albumsCustom.value.trim() : '')
                : (albumsSelect ? albumsSelect.value : '');
            if (albumsEl) {
                let p = musicRoot;
                if (albumsVal && albumsVal !== '.') p += '/' + albumsVal;
                p += '/Artist/Album/' + (includeTrackNumber ? '1 - Track.flac' : 'Track.flac');
                albumsEl.textContent = 'Files saved to: ' + p;
            }
        }

        function _injectSettingsClearButtons() {
            // Skip these - they already have dedicated clear mechanisms
            const skipIds = new Set([
                'settingYoutubeCookies', 'settingSpotifyCookies', 'settingSmtpPort', 'settingMinBitrate',
                'settingSpotifyBrowserTimeout', 'settingSpotifyBrowserStall',
                'customSubdirInput', 'customPlaylistsSubdirInput', 'customAlbumsSubdirInput'
            ]);

            for (const row of document.querySelectorAll('.settings-row')) {
                const input = row.querySelector('input[type="text"], input[type="password"], input[type="number"]');
                if (!input || skipIds.has(input.id)) continue;
                if (input.disabled) continue; // env-locked

                const clearBtn = document.createElement('button');
                clearBtn.type = 'button';
                clearBtn.className = 'btn btn-sm btn-danger setting-clear-btn';
                clearBtn.textContent = 'Clear';

                // Find the settings key for this input
                const settingsKey = Object.entries(settingsFieldMap).find(([, id]) => id === input.id)?.[0];

                clearBtn.addEventListener('click', async () => {
                    input.value = '';
                    if (settingsKey && sensitiveFields.includes(settingsKey)) {
                        input.dataset.forceClear = 'true';
                        input.placeholder = input.defaultValue || '';
                    }
                    await saveSettings();
                });

                // Wrap input (or password-field div) and clear button together
                const passwordField = row.querySelector('.password-field');
                const target = passwordField || input;

                const wrapper = document.createElement('div');
                wrapper.className = 'setting-input-group';
                target.parentNode.insertBefore(wrapper, target);
                wrapper.appendChild(target);
                wrapper.appendChild(clearBtn);
            }
        }

        function updateBrowserApiKeyStatus() {
            // In session mode the browser API key row isn't really relevant, but we keep
            // the element around for backward compatibility. Show a polite "N/A" message.
            const statusEl = document.getElementById('browserApiKeyStatus');
            const clearBtn = document.getElementById('clearApiKeyBtn');
            if (!statusEl) return;

            if (serverConfig && serverConfig.users_exist) {
                const user = getCurrentUser();
                statusEl.textContent = user ? `Signed in as ${user.username}` : 'Session mode';
                statusEl.style.color = 'var(--text-secondary)';
                if (clearBtn) clearBtn.style.display = 'none';
            } else {
                // Legacy single-user API key mode
                const storedKey = localStorage.getItem('apiKey') || '';
                if (storedKey) {
                    statusEl.textContent = `Key stored (${storedKey.length} chars)`;
                    statusEl.style.color = 'var(--success)';
                    if (clearBtn) clearBtn.style.display = 'inline-block';
                } else {
                    statusEl.textContent = 'No key stored';
                    statusEl.style.color = 'var(--text-secondary)';
                    if (clearBtn) clearBtn.style.display = 'none';
                }
            }
        }

        // Clear stored (legacy) API key button
        document.getElementById('clearApiKeyBtn')?.addEventListener('click', () => {
            localStorage.removeItem('apiKey');
            updateBrowserApiKeyStatus();
            showToast('Stored API key cleared');
        });

        function _syncNotifyOnCheckboxes() {
            const val = document.getElementById('settingNotifyOn')?.value || '';
            const parts = val.split(',').map(s => s.trim());
            document.getElementById('settingNotifyOnSingles').checked  = parts.includes('singles');
            document.getElementById('settingNotifyOnPlaylists').checked = parts.includes('playlists');
            document.getElementById('settingNotifyOnBulk').checked     = parts.includes('bulk');
            document.getElementById('settingNotifyOnErrors').checked   = parts.includes('errors');
        }

        function _syncNotifyOnHidden() {
            const parts = [];
            if (document.getElementById('settingNotifyOnSingles').checked)  parts.push('singles');
            if (document.getElementById('settingNotifyOnPlaylists').checked) parts.push('playlists');
            if (document.getElementById('settingNotifyOnBulk').checked)     parts.push('bulk');
            if (document.getElementById('settingNotifyOnErrors').checked)   parts.push('errors');
            document.getElementById('settingNotifyOn').value = parts.join(',');
        }

        async function saveSettings() {
            const saveBtn = document.getElementById('saveSettingsBtn');
            const resultDiv = document.getElementById('settingsSaveResult');

            saveBtn.disabled = true;
            saveBtn.textContent = 'Saving...';
            resultDiv.textContent = '';
            resultDiv.classList.remove('error');

            // Build the hidden notify_on value from checkboxes before reading it
            _syncNotifyOnHidden();

            try {
                const updates = {};

                for (const [key, elementId] of Object.entries(settingsFieldMap)) {
                    // Skip env-locked fields
                    if (envOverrides.includes(key)) continue;

                    const element = document.getElementById(elementId);
                    if (!element) continue;

                    if (element.type === 'checkbox') {
                        const currentValue = element.checked;
                        // Only include if changed from original
                        if (currentValue !== originalValues[key]) {
                            updates[key] = currentValue;
                        }
                    } else {
                        let value = element.value.trim();
                        // Singles subfolder: use direct picker value or custom path
                        if (key === 'singles_subdir') {
                            if (value === '.') {
                                value = '.';
                            } else if (value === SUBDIR_CUSTOM_VALUE) {
                                const customInput = document.getElementById('customSubdirInput');
                                value = _normaliseSubdirPath(customInput?.value || '') || 'Singles';
                            } else {
                                value = _normaliseSubdirPath(value) || 'Singles';
                            }
                            updates[key] = value;
                            continue;
                        }
                        // Playlists subfolder: same pattern, empty string = disabled
                        if (key === 'playlists_subdir') {
                            if (value === '' || value === '.') {
                                // keep as-is (empty = disabled, '.' = root)
                            } else if (value === SUBDIR_CUSTOM_VALUE) {
                                const customInput = document.getElementById('customPlaylistsSubdirInput');
                                value = _normaliseSubdirPath(customInput?.value || '') || 'Playlists';
                            } else {
                                value = _normaliseSubdirPath(value);
                            }
                            updates[key] = value;
                            continue;
                        }
                        // Albums subfolder: always enabled, defaults to 'Albums'
                        if (key === 'albums_subdir') {
                            if (value === '.') {
                                value = '.';
                            } else if (value === SUBDIR_CUSTOM_VALUE) {
                                const customInput = document.getElementById('customAlbumsSubdirInput');
                                value = _normaliseSubdirPath(customInput?.value || '') || 'Albums';
                            } else {
                                value = _normaliseSubdirPath(value) || 'Albums';
                            }
                            // Always include subdir fields; change detection fails when the
                            // saved value is itself a custom path (originalValues holds the
                            // resolved string, resolved value matches, gets skipped as "no change")
                            updates[key] = value;
                            continue;
                        }
                        // For sensitive fields with hidden values (null), only send if user entered something
                        if (sensitiveFields.includes(key)) {
                            if (originalValues[key] === null) {
                                // Field was "configured (hidden)" - only send if user typed something new
                                if (value && value !== '') {
                                    updates[key] = value;
                                } else if (element.dataset.forceClear === 'true') {
                                    updates[key] = '';
                                }
                            } else if (value !== originalValues[key]) {
                                // Field had visible value - send if changed (including to empty)
                                updates[key] = value;
                            }
                        } else {
                            // Non-sensitive field - only send if changed from original
                            if (value !== originalValues[key]) {
                                updates[key] = value;
                            }
                        }
                    }
                }

                // Don't make API call if nothing changed
                if (Object.keys(updates).length === 0) {
                    resultDiv.textContent = 'No changes to save';
                    saveBtn.disabled = false;
                    saveBtn.textContent = 'Save Settings';
                    return;
                }

                const response = await apiFetch('/api/settings', {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(updates)
                });

                if (!response.ok) throw new Error('Failed to save settings');

                const data = await response.json();
                resultDiv.textContent = `Saved ${data.updated.length} setting(s)`;
                showToast('Settings saved');

                // Refresh Spotify cookie status (expired banner + expiry hint)
                if (data.settings) _updateSpotifyCookieHint(data.settings);

                // Update original values and clear sensitive fields after save
                for (const [key, value] of Object.entries(updates)) {
                    if (sensitiveFields.includes(key)) {
                        const elementId = settingsFieldMap[key];
                        if (elementId) {
                            const element = document.getElementById(elementId);
                            if (element && element.type !== 'checkbox') {
                                if (value === '') {
                                    originalValues[key] = '';
                                    element.value = '';
                                    element.placeholder = element.dataset.defaultPlaceholder || '';
                                } else {
                                    // Mark as "configured but hidden" and clear the field
                                    originalValues[key] = null;
                                    element.value = '';
                                    element.placeholder = 'Configured (hidden)';
                                }
                                element.dataset.forceClear = '';
                            }
                        }
                    } else {
                        // Update tracked original value
                        originalValues[key] = value;

                        // Sync header toggle when default_convert_audio is saved
                        if (key === 'default_convert_audio') {
                            convertAudioCheckbox.checked = value;
                            localStorage.setItem(userStorageKey('convertAudio'), value);
                            if (watchedConvertAudio && !watchedConversionTouched) {
                                watchedConvertAudio.checked = value;
                            }
                            setDefaultConversionMode(value);
                            updateConversionLabels();
                        }
                        // Sync format picker when audio_format is saved
                        if (key === 'audio_format') {
                            setAudioFormat(value);
                        }
                        // Sync playlists_subdir in serverConfig so watched/bulk-import UI updates
                        if (key === 'playlists_subdir') {
                            serverConfig.playlists_subdir = value;
                            const watchedToggle = document.getElementById('watchedPlaylistsDirToggle');
                            if (watchedToggle) watchedToggle.style.display = value ? 'flex' : 'none';
                            const watchedUsePlaylistsDir = document.getElementById('watchedUsePlaylistsDir');
                            if (watchedUsePlaylistsDir) watchedUsePlaylistsDir.checked = !!value;
                            const usePlaylistsDirCheckbox = document.getElementById('usePlaylistsDirCheckbox');
                            if (usePlaylistsDirCheckbox && value) usePlaylistsDirCheckbox.checked = true;
                        }
                    }
                }
            } catch (error) {
                console.error('Failed to save settings:', error);
                resultDiv.textContent = 'Failed to save settings';
                resultDiv.classList.add('error');
                showToast('Failed to save settings', true);
            } finally {
                saveBtn.disabled = false;
                saveBtn.textContent = 'Save Settings';
            }
        }

        async function testConnection(service) {
            const idMap = {
                'youtube-cookies': { btn: 'testYoutubeCookiesBtn', result: 'youtubeCookiesTestResult', label: 'Test Cookies' },
                'spotify-cookies': { btn: 'testSpotifyCookiesBtn', result: 'spotifyCookiesTestResult', label: 'Test Cookies' },
                'apprise': { btn: 'testAppriseBtn', result: 'appriseTestResult', label: 'Test Apprise' },
                'email': { btn: 'testEmailBtn', result: 'emailTestResult', label: 'Send Test Email' },
            };
            const ids = idMap[service] || { btn: `test${service.charAt(0).toUpperCase() + service.slice(1)}Btn`, result: `${service}TestResult`, label: 'Test Connection' };
            const btn = document.getElementById(ids.btn);
            const resultDiv = document.getElementById(ids.result);

            const btnLabel = ids.label;
            btn.disabled = true;
            btn.textContent = 'Testing...';
            resultDiv.className = 'test-result';
            resultDiv.style.display = 'none';
            // Reset Navidrome real-path status on each new test
            if (service === 'navidrome') {
                const rpDiv = document.getElementById('navidromeRealPathStatus');
                if (rpDiv) rpDiv.style.display = 'none';
            }

            // Gather current form values to test with (before saving)
            let body = {};
            if (service === 'slskd') {
                body = {
                    url: document.getElementById('settingSlskdUrl').value.trim(),
                    username: document.getElementById('settingSlskdUser').value.trim(),
                    password: document.getElementById('settingSlskdPass').value.trim(),
                    downloads_path: document.getElementById('settingSlskdDownloads').value.trim()
                };
            } else if (service === 'navidrome') {
                body = {
                    url: document.getElementById('settingNavidromeUrl').value.trim(),
                    username: document.getElementById('settingNavidromeUser').value.trim(),
                    password: document.getElementById('settingNavidromePass').value.trim()
                };
            } else if (service === 'jellyfin') {
                body = {
                    url: document.getElementById('settingJellyfinUrl').value.trim(),
                    api_key: document.getElementById('settingJellyfinApiKey').value.trim()
                };
            } else if (service === 'plex') {
                body = {
                    url: document.getElementById('settingPlexUrl').value.trim(),
                    token: document.getElementById('settingPlexToken').value.trim()
                };
            } else if (service === 'lidarr') {
                body = {
                    url: document.getElementById('settingLidarrUrl').value.trim(),
                    api_key: document.getElementById('settingLidarrApiKey').value.trim()
                };
            } else if (service === 'youtube-cookies') {
                body = {
                    cookies: document.getElementById('settingYoutubeCookies').value
                };
            } else if (service === 'spotify-cookies') {
                body = {
                    cookies: document.getElementById('settingSpotifyCookies').value
                };
            } else if (service === 'apprise') {
                body = {
                    url: document.getElementById('settingAppriseUrl').value.trim()
                };
            } else if (service === 'email') {
                body = {
                    smtp_host: document.getElementById('settingSmtpHost').value.trim(),
                    smtp_port: Number(document.getElementById('settingSmtpPort').value),
                    smtp_user: document.getElementById('settingSmtpUser').value.trim(),
                    smtp_from: document.getElementById('settingSmtpFrom').value.trim(),
                    smtp_to: document.getElementById('settingSmtpTo').value.trim(),
                    smtp_tls: document.getElementById('settingSmtpTls').checked
                };
                const smtpPassword = document.getElementById('settingSmtpPass').value;
                // A blank password field can mean "configured, but masked". Let
                // the server use the saved secret unless a replacement was typed.
                if (smtpPassword) body.smtp_pass = smtpPassword;
            }

            try {
                const response = await apiFetch(`/api/settings/test/${service}`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(body)
                });

                const data = await response.json();

                if (data.success) {
                    resultDiv.textContent = data.message;
                    resultDiv.className = data.warning ? 'test-result warning' : 'test-result success';
                    // For Navidrome, show a second status line about real path support
                    if (service === 'navidrome') {
                        const rpDiv = document.getElementById('navidromeRealPathStatus');
                        if (rpDiv) {
                            if (data.real_path === true) {
                                rpDiv.textContent = 'Real file paths enabled; M3U playlist entries will use accurate paths';
                                rpDiv.className = 'test-result success';
                            } else {
                                rpDiv.textContent = data.real_path_hint || '';
                                rpDiv.className = 'test-result warning';
                            }
                            rpDiv.style.display = 'block';
                        }
                    }
                } else {
                    resultDiv.textContent = data.message || 'Connection failed';
                    resultDiv.className = 'test-result error';
                }
                resultDiv.style.display = 'block';
            } catch (error) {
                resultDiv.textContent = 'Connection test failed';
                resultDiv.className = 'test-result error';
                resultDiv.style.display = 'block';
            } finally {
                btn.disabled = false;
                btn.textContent = btnLabel;
            }
        }

        // The Monochrome test reports a whole ladder of legs rather than a single
        // yes/no, so it gets its own renderer instead of squeezing into testConnection.
        async function testMonochromeDownloads() {
            const btn = document.getElementById('testMonochromeBtn');
            const resultDiv = document.getElementById('monochromeTestResult');
            const stepsDiv = document.getElementById('monochromeTestSteps');

            btn.disabled = true;
            btn.textContent = 'Testing (this can take a minute)...';
            resultDiv.className = 'test-result';
            resultDiv.style.display = 'none';
            stepsDiv.hidden = true;
            stepsDiv.innerHTML = '';

            try {
                const response = await apiFetch('/api/settings/test/monochrome', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: '{}'
                });
                const data = await response.json();

                const steps = Array.isArray(data.steps) ? data.steps : [];
                const anyWarning = steps.some(step => step.status === 'warn');
                resultDiv.textContent = data.elapsed
                    ? `${data.message} (${data.elapsed}s)`
                    : (data.message || 'Test failed');
                resultDiv.className = data.success
                    ? (anyWarning ? 'test-result warning' : 'test-result success')
                    : 'test-result error';
                resultDiv.style.display = 'block';

                if (steps.length || data.hint) {
                    stepsDiv.innerHTML = '';
                    steps.forEach(step => {
                        const row = document.createElement('div');
                        row.className = `diag-step ${step.status || 'skip'}`;
                        const status = document.createElement('span');
                        status.className = 'diag-step-status';
                        status.textContent = step.status || '';
                        const body = document.createElement('div');
                        body.className = 'diag-step-body';
                        const name = document.createElement('span');
                        name.className = 'diag-step-name';
                        name.textContent = step.name || '';
                        body.appendChild(name);
                        if (step.detail) {
                            const detail = document.createElement('span');
                            detail.className = 'diag-step-detail';
                            detail.textContent = step.detail;
                            body.appendChild(detail);
                        }
                        row.appendChild(status);
                        row.appendChild(body);
                        stepsDiv.appendChild(row);
                    });
                    if (data.hint) {
                        const hint = document.createElement('div');
                        hint.className = 'diag-hint';
                        hint.textContent = `Likely cause: ${data.hint}`;
                        stepsDiv.appendChild(hint);
                    }
                    stepsDiv.hidden = false;
                }
            } catch (error) {
                resultDiv.textContent = 'Monochrome test failed to run  -  check the server logs';
                resultDiv.className = 'test-result error';
                resultDiv.style.display = 'block';
            } finally {
                btn.disabled = false;
                btn.textContent = 'Test Downloads';
            }
        }

        // Password field toggle
        document.querySelectorAll('.password-toggle').forEach(btn => {
            btn.addEventListener('click', () => {
                const targetId = btn.dataset.target;
                const input = document.getElementById(targetId);
                if (input) {
                    if (input.type === 'password') {
                        input.type = 'text';
                        btn.textContent = 'Hide';
                    } else {
                        input.type = 'password';
                        btn.textContent = 'Show';
                    }
                }
            });
        });

        // Settings event listeners
        document.getElementById('saveSettingsBtn').addEventListener('click', saveSettings);
        document.getElementById('testSlskdBtn').addEventListener('click', () => testConnection('slskd'));
        document.getElementById('testNavidromeBtn').addEventListener('click', () => testConnection('navidrome'));
        document.getElementById('testJellyfinBtn').addEventListener('click', () => testConnection('jellyfin'));
        document.getElementById('testPlexBtn').addEventListener('click', () => testConnection('plex'));
        document.getElementById('testLidarrBtn').addEventListener('click', () => testConnection('lidarr'));
        document.getElementById('testYoutubeCookiesBtn').addEventListener('click', () => testConnection('youtube-cookies'));
        document.getElementById('testSpotifyCookiesBtn').addEventListener('click', () => testConnection('spotify-cookies'));
        document.getElementById('testMonochromeBtn').addEventListener('click', testMonochromeDownloads);
        document.getElementById('testAppriseBtn').addEventListener('click', () => testConnection('apprise'));
        document.getElementById('testEmailBtn').addEventListener('click', () => testConnection('email'));
        const uploadYoutubeCookiesBtn = document.getElementById('uploadYoutubeCookiesBtn');
        const youtubeCookiesFile = document.getElementById('youtubeCookiesFile');
        const youtubeCookiesTextarea = document.getElementById('settingYoutubeCookies');
        const clearYoutubeCookiesBtn = document.getElementById('clearYoutubeCookiesBtn');

        uploadYoutubeCookiesBtn?.addEventListener('click', () => {
            if (envOverrides.includes('youtube_cookies')) {
                showToast('YouTube cookies are locked by environment settings', true);
                return;
            }
            youtubeCookiesFile?.click();
        });

        youtubeCookiesFile?.addEventListener('change', async (event) => {
            const file = event.target.files?.[0];
            if (!file) return;
            try {
                const text = await file.text();
                if (youtubeCookiesTextarea) {
                    youtubeCookiesTextarea.value = text;
                }
                showToast('Cookies loaded, saving...');
                await saveSettings();
                showToast('Cookies updated');
                _updateCookieExpiryHint();
            } catch (error) {
                console.error('Failed to read cookies file:', error);
                showToast('Failed to read cookies file', true);
            } finally {
                event.target.value = '';
            }
        });

        clearYoutubeCookiesBtn?.addEventListener('click', async () => {
            if (envOverrides.includes('youtube_cookies')) {
                showToast('YouTube cookies are locked by environment settings', true);
                return;
            }
            if (!youtubeCookiesTextarea) return;
            youtubeCookiesTextarea.value = '';
            youtubeCookiesTextarea.dataset.forceClear = 'true';
            showToast('Clearing cookies...');
            await saveSettings();
            _updateCookieExpiryHint();
        });

        const uploadSpotifyCookiesBtn = document.getElementById('uploadSpotifyCookiesBtn');
        const spotifyCookiesFile = document.getElementById('spotifyCookiesFile');
        const spotifyCookiesTextarea = document.getElementById('settingSpotifyCookies');
        const clearSpotifyCookiesBtn = document.getElementById('clearSpotifyCookiesBtn');

        uploadSpotifyCookiesBtn?.addEventListener('click', () => {
            spotifyCookiesFile?.click();
        });

        spotifyCookiesFile?.addEventListener('change', async (event) => {
            const file = event.target.files?.[0];
            if (!file) return;
            try {
                const text = await file.text();
                if (spotifyCookiesTextarea) {
                    spotifyCookiesTextarea.value = text;
                }
                showToast('Cookies loaded, saving...');
                await saveSettings();
                showToast('Spotify cookies updated');
            } catch (error) {
                console.error('Failed to read Spotify cookies file:', error);
                showToast('Failed to read cookies file', true);
            } finally {
                event.target.value = '';
            }
        });

        clearSpotifyCookiesBtn?.addEventListener('click', async () => {
            if (!spotifyCookiesTextarea) return;
            spotifyCookiesTextarea.value = '';
            spotifyCookiesTextarea.dataset.forceClear = 'true';
            showToast('Clearing Spotify cookies...');
            await saveSettings();
            _updateSpotifyCookieHint(null);
        });

        // Audio format segmented buttons (FLAC/Opus) and header toggle are wired
        // through setAudioFormat() - no direct sync needed here.

        // =============================================================================
        // Multi-User UI
        // =============================================================================

        function applyUserRoleToUI() {
            const admin = isAdmin();
            const peon = isPeon();

            // Toggle visibility of admin-only sections (set inline, no CSS class needed)
            document.querySelectorAll('.admin-only').forEach(el => {
                el.style.display = admin ? '' : 'none';
            });

            // Peons are kept on the happy path: no Settings, no Stats. Anything
            // else marked .peon-hide vanishes for them too. If they were sat on
            // a now-hidden tab, bounce them back to Results.
            document.querySelectorAll('.peon-hide').forEach(el => {
                el.style.display = peon ? 'none' : '';
            });
            if (peon) {
                const activeTab = document.querySelector('.tab.active');
                if (activeTab && activeTab.classList.contains('peon-hide')) {
                    const resultsTabBtn = document.querySelector('.tab[data-tab="results"]');
                    if (resultsTabBtn) resultsTabBtn.click();
                }
                // Pin the convert-to-format checkboxes to whatever the admin set
                // globally. The server overrides these on submit either way, but
                // mirroring the value here keeps the (hidden) UI honest if anyone
                // peeks via dev tools.
                const adminDefault = !!(serverConfig && (serverConfig.default_convert_audio ?? serverConfig.default_convert_to_flac));
                const conversion = document.getElementById('convertAudio');
                const watchedFlac = document.getElementById('watchedConvertAudio');
                if (conversion) conversion.checked = adminDefault;
                if (watchedFlac) watchedFlac.checked = adminDefault;
            }

            // Show logout button only in session mode
            const logoutBtn = document.getElementById('logoutBtn');
            if (logoutBtn) {
                logoutBtn.style.display = serverConfig && serverConfig.users_exist ? '' : 'none';
            }

            // Show current username in header if in session mode
            const userDisplay = document.getElementById('currentUserDisplay');
            if (userDisplay) {
                const user = getCurrentUser();
                if (user && serverConfig && serverConfig.users_exist) {
                    userDisplay.textContent = user.username;
                    userDisplay.style.display = '';
                } else {
                    userDisplay.style.display = 'none';
                }
            }

            // Show the "Change my password" button only when in session mode
            const changePwSection = document.getElementById('changePasswordSection');
            if (changePwSection) {
                changePwSection.style.display = (serverConfig && serverConfig.users_exist) ? '' : 'none';
            }

            // Singles-only mode used to hide the whole Albums tab; now that album
            // browsing lives inside Artists (alongside singles-following, which
            // singles-only mode should NOT hide), just tuck away the album bits:
            // the one-off browser and, per card, the per-artist album panel.
            const singlesOnlyMode = !!(serverConfig && serverConfig.singles_only_mode);
            // Two of the three chooser options are album work, so they go; "Follow
            // for Singles" stays, which leaves the chooser a slightly lonely but
            // perfectly functional one-button affair.
            document.querySelectorAll('#artistModeChooser [data-artist-mode]').forEach(btn => {
                if (btn.dataset.artistMode !== 'singles') {
                    btn.style.display = singlesOnlyMode ? 'none' : '';
                }
            });
            if (singlesOnlyMode) {
                for (const id of ['artistAlbumsFollowPanel', 'artistOneOffPanel']) {
                    const el = document.getElementById(id);
                    if (el) el.style.display = 'none';
                }
            }
            const artistAlbumPanel = document.getElementById('artistAlbumPanel');
            if (singlesOnlyMode && artistAlbumPanel) artistAlbumPanel.style.display = 'none';
            // Bulk Import hosts the same browser, so it hides on the same terms;
            // otherwise singles-only mode is just a suggestion with a side door.
            const bulkAlbumBrowseWrap = document.getElementById('bulkAlbumBrowseWrap');
            if (bulkAlbumBrowseWrap) bulkAlbumBrowseWrap.style.display = singlesOnlyMode ? 'none' : '';
        }

        document.getElementById('userInfoToggle')?.addEventListener('click', () => {
            const panel = document.getElementById('userInfoPanel');
            const btn = document.getElementById('userInfoToggle');
            if (!panel || !btn) return;
            const open = panel.hasAttribute('hidden');
            if (open) {
                panel.removeAttribute('hidden');
                btn.setAttribute('aria-expanded', 'true');
            } else {
                panel.setAttribute('hidden', '');
                btn.setAttribute('aria-expanded', 'false');
            }
        });

        async function loadUsers() {
            if (!isAdmin()) return;
            try {
                const resp = await apiFetch('/api/users');
                if (!resp.ok) return;
                const data = await resp.json();
                const listEl = document.getElementById('userList');
                const warningEl = document.getElementById('firstUserWarning');
                const roleEl = document.getElementById('newUserRole');
                if (!listEl) return;
                const currentUser = getCurrentUser();
                const noUsers = !data.users || data.users.length === 0;

                // Show the "point of no return" warning and lock role to Admin when
                // no users exist yet; the first account must always be admin.
                if (warningEl) warningEl.style.display = noUsers ? 'block' : 'none';
                if (roleEl) {
                    if (noUsers) roleEl.value = 'admin';
                    Array.from(roleEl.options).forEach(o => {
                        o.disabled = noUsers && o.value !== 'admin';
                    });
                }

                listEl.innerHTML = data.users.map(u => `
                    <div style="display:flex; align-items:center; gap:12px; padding:10px 0; border-bottom:1px solid var(--border);">
                        <span style="flex:1; font-weight:${u.id === currentUser?.id ? '600' : '400'};">${escapeHtml(u.username)}</span>
                        <span style="color:var(--text-secondary); font-size:13px;">${u.role}</span>
                        ${u.id !== currentUser?.id
                            ? `<button class="user-action-btn warning" onclick="forcePasswordReset('${escapeAttr(u.id)}', '${escapeAttr(u.username)}')">Force reset</button>
                               <button class="user-action-btn" onclick="deleteUser('${escapeAttr(u.id)}', '${escapeAttr(u.username)}')">Remove</button>`
                            : `<span style="color:var(--text-secondary); font-size:13px;">(you)</span>`}
                    </div>
                `).join('') || '<p style="color:var(--text-secondary); font-size:13px;">No users yet.</p>';
            } catch {}
        }

        async function createUser() {
            const username = document.getElementById('newUserUsername').value.trim();
            const password = document.getElementById('newUserPassword').value;
            const role = document.getElementById('newUserRole').value;
            const errorEl = document.getElementById('createUserError');

            if (!username || !password) {
                errorEl.textContent = 'Username and password are required.';
                errorEl.style.display = 'block';
                return;
            }

            try {
                const resp = await apiFetch('/api/users', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ username, password, role }),
                });
                const data = await resp.json().catch(() => ({}));
                if (!resp.ok) {
                    errorEl.textContent = data.detail || 'Failed to create user.';
                    errorEl.style.display = 'block';
                    return;
                }
                errorEl.style.display = 'none';
                document.getElementById('newUserUsername').value = '';
                document.getElementById('newUserPassword').value = '';
                // Going from 0 to 1 users (first_user) or 1 to 2 (requires_login)
                // both flip into session-required mode. The current admin has no
                // session, so further requests will 401; clear state and bounce
                // to login rather than leaving the UI stuck on a stale view.
                if (data.first_user || data.requires_login) {
                    localStorage.clear();
                    sessionStorage.clear();
                    window.location.reload();
                    return;
                }
                await loadUsers();
            } catch {
                errorEl.textContent = 'Error creating user.';
                errorEl.style.display = 'block';
            }
        }

        async function deleteUser(userId, username) {
            if (!confirm(`Remove user "${username}"? This cannot be undone.`)) return;
            try {
                const resp = await apiFetch(`/api/users/${userId}`, { method: 'DELETE' });
                if (!resp.ok) {
                    const data = await resp.json().catch(() => ({}));
                    alert(data.detail || 'Failed to remove user.');
                    return;
                }
                await loadUsers();
            } catch {
                alert('Error removing user.');
            }
        }

        async function forcePasswordReset(userId, username) {
            if (!confirm(`Force "${username}" to set a new password on next login?\n\nTheir current sessions will be terminated immediately.`)) return;
            try {
                const resp = await apiFetch(`/api/users/${userId}/force-password-change`, { method: 'PUT' });
                if (!resp.ok) {
                    const data = await resp.json().catch(() => ({}));
                    alert(data.detail || 'Failed to flag user for password reset.');
                    return;
                }
                await loadUsers();
            } catch {
                alert('Error flagging user for password reset.');
            }
        }

        // Destination picker - initialise on page load so it's ready before the first search
        initDestinationPicker();
            missingTrackVersionsState.mode = 'watched';
            missingTrackVersionsState.jobId = '';
