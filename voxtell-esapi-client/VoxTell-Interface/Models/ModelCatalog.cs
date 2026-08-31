using System;
using System.Collections.Generic;
using System.Linq;
using Newtonsoft.Json;

namespace VoxTell_Interface.Models
{
    /// <summary>
    /// What this deployment can be asked to segment: models, structures, groups
    /// and presets, fetched from <c>GET /v1/models</c>.
    ///
    /// Why this is fetched and not compiled in
    /// ---------------------------------------
    /// Eclipse approves a plugin DLL by version **and** content hash, on every
    /// workstation. Anything baked into this assembly therefore costs a
    /// clinic-wide re-approval to change, and a physicist's time. A catalog read
    /// at runtime costs nothing, so adding a model — or fixing a structure name —
    /// becomes a server-side deployment rather than a plugin release.
    ///
    /// The plugin deliberately holds no opinion about which models exist. There
    /// is no hard-coded list of CADS tasks here, and none should be added.
    /// </summary>
    public sealed class ModelCatalog
    {
        [JsonProperty("version")]
        public int Version { get; set; }

        /// <summary>Render structure groups in this order. Server decides.</summary>
        [JsonProperty("group_order")]
        public List<string> GroupOrder { get; set; }

        [JsonProperty("models")]
        public List<CatalogModel> Models { get; set; }

        [JsonProperty("structures")]
        public List<CatalogStructure> Structures { get; set; }

        [JsonProperty("presets")]
        public List<CatalogPreset> Presets { get; set; }

        /// <summary>
        /// Clinic protocols: a named structure set with the naming the clinic actually
        /// uses. Served rather than compiled in, and served rather than read from Eclipse
        /// — ESAPI 16.1 exposes no structure-template or clinical-protocol enumeration at
        /// all, so a protocol is ours to define.
        ///
        /// Why this matters more than a preset: a preset only says *what* to segment, and
        /// left the write-as id, the DICOM type and the colour to be typed per row on
        /// every run. Naming drift is the failure the published audits blame for silently
        /// losing cases, and a protocol is where a physicist fixes it once.
        /// </summary>
        [JsonProperty("protocols")]
        public List<CatalogProtocol> Protocols { get; set; }

        public bool HasProtocols
        {
            get { return Protocols != null && Protocols.Count > 0; }
        }

        // --- derived lookups ------------------------------------------------ //
        // Built once on demand rather than in a constructor, because Newtonsoft
        // populates the properties after construction.

        private Dictionary<string, CatalogStructure> _byId;
        private Dictionary<string, CatalogStructure> _byAlias;

        /// <summary>Structure by its namespaced id, or null.</summary>
        public CatalogStructure Structure(string id)
        {
            BuildIndex();
            CatalogStructure found;
            return id != null && _byId.TryGetValue(id, out found) ? found : null;
        }

        /// <summary>Protocol by key, or null.</summary>
        public CatalogProtocol Protocol(string key)
        {
            if (key == null || Protocols == null) return null;
            return Protocols.FirstOrDefault(
                p => string.Equals(p.Key, key, StringComparison.Ordinal));
        }

        /// <summary>
        /// Split a protocol's entries into the ones this deployment can actually produce
        /// and the ones it cannot.
        ///
        /// The second list is returned rather than filtered away on purpose. A protocol
        /// entry no model produces is exactly the case a clinic has to see: silently
        /// dropping it produces a run that looks complete and is missing a structure.
        /// </summary>
        public void SplitEntries(
            CatalogProtocol protocol,
            out IList<ProtocolEntry> available,
            out IList<ProtocolEntry> unavailable)
        {
            available = new List<ProtocolEntry>();
            unavailable = new List<ProtocolEntry>();
            if (protocol == null || protocol.Entries == null) return;

            foreach (ProtocolEntry entry in protocol.Entries)
            {
                if (entry == null || string.IsNullOrEmpty(entry.StructureId)) continue;
                if (Structure(entry.StructureId) != null) available.Add(entry);
                else unavailable.Add(entry);
            }
        }

        /// <summary>Model by key, or null.</summary>
        public CatalogModel Model(string key)
        {
            if (key == null || Models == null) return null;
            return Models.FirstOrDefault(
                m => string.Equals(m.Key, key, StringComparison.Ordinal));
        }

        /// <summary>
        /// Resolve a free-form structure name — in practice an ESAPI
        /// <c>Structure.Id</c> — to a catalog structure, or null.
        ///
        /// Returns null rather than guessing. An unmatched name is shown to the
        /// planner as unmatched: silently dropping structures whose names are off
        /// the local convention is the single commonest failure reported in the
        /// published auto-segmentation audits, and it fails invisibly.
        /// </summary>
        public CatalogStructure Resolve(string name)
        {
            BuildIndex();
            string key = Normalise(name);
            if (key.Length == 0) return null;
            CatalogStructure found;
            return _byAlias.TryGetValue(key, out found) ? found : null;
        }

        /// <summary>
        /// Match key for a structure name: lowercase, alphanumerics only.
        ///
        /// Punctuation and separators carry no clinical meaning in a structure
        /// name, so <c>Kidney_R</c>, <c>Kidney R</c> and <c>kidney-r</c> must all
        /// resolve to the same structure.
        ///
        /// This is one third of a contract written in three places: it must stay
        /// behaviourally identical to <c>normalise()</c> in
        /// <c>voxtell_cloud/catalog.py</c> and to <c>norm()</c> in
        /// <c>scripts/gen_catalog.py</c>. The server stores its aliases already
        /// normalised by that rule; if these two ever disagree, auto-detect stops
        /// matching and nothing raises an error.
        /// </summary>
        public static string Normalise(string name)
        {
            if (string.IsNullOrEmpty(name)) return string.Empty;
            var sb = new System.Text.StringBuilder(name.Length);
            foreach (char c in name)
            {
                // ASCII-only on purpose, to match Python's [^a-z0-9] exactly.
                if (c >= '0' && c <= '9') sb.Append(c);
                else if (c >= 'a' && c <= 'z') sb.Append(c);
                else if (c >= 'A' && c <= 'Z') sb.Append((char)(c + 32));
            }
            return sb.ToString();
        }

        /// <summary>Structures produced by one model, in catalog order.</summary>
        public IList<CatalogStructure> StructuresFor(string modelKey)
        {
            if (Structures == null) return new List<CatalogStructure>();
            return Structures
                .Where(s => string.Equals(s.SourceModel, modelKey, StringComparison.Ordinal))
                .ToList();
        }

        /// <summary>
        /// Structures grouped for display, groups in <see cref="GroupOrder"/>.
        /// Any group the server did not order is appended alphabetically rather
        /// than dropped — a structure that exists must always be reachable.
        /// </summary>
        public IList<KeyValuePair<string, IList<CatalogStructure>>> Grouped(
            IEnumerable<CatalogStructure> subset = null)
        {
            var items = (subset ?? Structures ?? Enumerable.Empty<CatalogStructure>()).ToList();
            var order = GroupOrder ?? new List<string>();

            var buckets = items
                .GroupBy(s => s.Group ?? string.Empty)
                .ToDictionary(g => g.Key, g => (IList<CatalogStructure>)g.ToList());

            var result = new List<KeyValuePair<string, IList<CatalogStructure>>>();
            foreach (string group in order)
            {
                IList<CatalogStructure> bucket;
                if (buckets.TryGetValue(group, out bucket))
                {
                    result.Add(new KeyValuePair<string, IList<CatalogStructure>>(group, bucket));
                    buckets.Remove(group);
                }
            }
            foreach (var leftover in buckets.OrderBy(kv => kv.Key, StringComparer.Ordinal))
            {
                result.Add(new KeyValuePair<string, IList<CatalogStructure>>(
                    leftover.Key, leftover.Value));
            }
            return result;
        }

        private void BuildIndex()
        {
            if (_byId != null) return;

            _byId = new Dictionary<string, CatalogStructure>(StringComparer.Ordinal);
            _byAlias = new Dictionary<string, CatalogStructure>(StringComparer.Ordinal);

            foreach (CatalogStructure s in Structures ?? new List<CatalogStructure>())
            {
                if (string.IsNullOrEmpty(s.Id)) continue;
                _byId[s.Id] = s;

                // The structure's own id and display name are always matchable,
                // on top of whatever aliases the server sent.
                AddAlias(Normalise(s.DisplayName), s);
                foreach (string alias in s.Aliases ?? new List<string>())
                {
                    AddAlias(alias, s);
                }
            }
        }

        // First writer wins. The server already refuses to load a catalog where
        // one alias is claimed by two structures, so a collision here can only
        // come from the display-name fallback above; keeping the earlier entry
        // makes the outcome deterministic rather than load-order dependent.
        private void AddAlias(string key, CatalogStructure s)
        {
            if (string.IsNullOrEmpty(key)) return;
            if (!_byAlias.ContainsKey(key)) _byAlias[key] = s;
        }
    }

    public sealed class CatalogModel
    {
        [JsonProperty("key")]
        public string Key { get; set; }

        [JsonProperty("display_name")]
        public string DisplayName { get; set; }

        /// <summary>"prompt" takes free text; anything else takes structure ids.</summary>
        [JsonProperty("kind")]
        public string Kind { get; set; }

        [JsonProperty("region")]
        public string Region { get; set; }

        [JsonProperty("modality")]
        public string Modality { get; set; }

        [JsonProperty("count")]
        public int? Count { get; set; }

        [JsonProperty("task")]
        public string Task { get; set; }

        [JsonProperty("weights_variant")]
        public string WeightsVariant { get; set; }

        /// <summary>
        /// Licence of the deployed weights. Shown to the planner, not decoration:
        /// CADS publishes three variants under three different licences and only
        /// one permits commercial use, so which one produced a given contour is a
        /// question a clinic may have to answer later.
        /// </summary>
        [JsonProperty("weights_licence")]
        public string WeightsLicence { get; set; }

        [JsonProperty("code_licence")]
        public string CodeLicence { get; set; }

        public bool TakesPrompts
        {
            get { return string.Equals(Kind, "prompt", StringComparison.Ordinal); }
        }
    }

    public sealed class CatalogStructure
    {
        [JsonProperty("id")]
        public string Id { get; set; }

        [JsonProperty("display_name")]
        public string DisplayName { get; set; }

        [JsonProperty("group")]
        public string Group { get; set; }

        [JsonProperty("modality")]
        public string Modality { get; set; }

        [JsonProperty("source_model")]
        public string SourceModel { get; set; }

        /// <summary>Already-normalised match keys. See <see cref="ModelCatalog.Normalise"/>.</summary>
        [JsonProperty("aliases")]
        public List<string> Aliases { get; set; }
    }

    /// <summary>
    /// A named structure set as a clinic writes it: which structures, under which ids,
    /// with which DICOM type and colour.
    /// </summary>
    public sealed class CatalogProtocol
    {
        [JsonProperty("key")]
        public string Key { get; set; }

        [JsonProperty("display_name")]
        public string DisplayName { get; set; }

        /// <summary>Treatment site, used only to group the protocol list.</summary>
        [JsonProperty("site")]
        public string Site { get; set; }

        [JsonProperty("modality")]
        public string Modality { get; set; }

        [JsonProperty("models")]
        public List<string> Models { get; set; }

        [JsonProperty("entries")]
        public List<ProtocolEntry> Entries { get; set; }

        public int Count
        {
            get { return Entries == null ? 0 : Entries.Count; }
        }
    }

    /// <summary>One structure in a protocol.</summary>
    public sealed class ProtocolEntry
    {
        [JsonProperty("structure_id")]
        public string StructureId { get; set; }

        /// <summary>
        /// The Eclipse structure Id to write. Capped at 16 characters by Eclipse itself,
        /// which is why the server refuses to serve a protocol that breaks the limit —
        /// discovering it here would mean discovering it during a write.
        /// </summary>
        [JsonProperty("write_as")]
        public string WriteAs { get; set; }

        [JsonProperty("dicom_type")]
        public string DicomType { get; set; }

        /// <summary>Hex, <c>#RRGGBB</c>. Null leaves the palette to choose.</summary>
        [JsonProperty("colour")]
        public string Colour { get; set; }

        [JsonProperty("required")]
        public bool Required { get; set; }

        /// <summary>The write-as id, or null when the server sent something unusable.</summary>
        public string SafeWriteAs
        {
            get
            {
                if (string.IsNullOrWhiteSpace(WriteAs)) return null;
                string trimmed = WriteAs.Trim();
                return trimmed.Length > 16 ? null : trimmed;
            }
        }

        /// <summary>
        /// The colour as three channels, or false when absent or unparseable.
        ///
        /// Bytes rather than a <c>System.Windows.Media.Color</c> on purpose: this file is
        /// linked into the offline harness, which references no WPF assemblies, and a
        /// catalog DTO has no business needing a presentation type. The caller builds the
        /// colour.
        ///
        /// Parsed rather than trusted: a bad hex string in a served catalog must degrade to
        /// "the palette picks one", never to an exception on the render path.
        /// </summary>
        public bool TryColour(out byte r, out byte g, out byte b)
        {
            r = 0; g = 0; b = 0;
            if (string.IsNullOrWhiteSpace(Colour)) return false;

            string hex = Colour.Trim().TrimStart('#');
            if (hex.Length != 6) return false;

            int value;
            if (!int.TryParse(hex, System.Globalization.NumberStyles.HexNumber,
                    System.Globalization.CultureInfo.InvariantCulture, out value))
            {
                return false;
            }

            r = (byte)((value >> 16) & 0xFF);
            g = (byte)((value >> 8) & 0xFF);
            b = (byte)(value & 0xFF);
            return true;
        }
    }

    public sealed class CatalogPreset
    {
        [JsonProperty("key")]
        public string Key { get; set; }

        [JsonProperty("display_name")]
        public string DisplayName { get; set; }

        [JsonProperty("structure_ids")]
        public List<string> StructureIds { get; set; }

        [JsonProperty("models")]
        public List<string> Models { get; set; }
    }
}
