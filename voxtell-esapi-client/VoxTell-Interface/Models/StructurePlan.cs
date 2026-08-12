using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Runtime.CompilerServices;
using System.Windows.Media;

namespace VoxTell_Interface.Models
{
    /// <summary>
    /// One row of the review list: how an incoming result maps onto the structure set, before
    /// anything is written into the patient.
    ///
    /// This lived inside <c>EsapiStructureImporter.cs</c>, which made it unreachable from any
    /// project that cannot reference the Varian assemblies — including the preview harness that
    /// exists to render the review list without Eclipse. Moving it out needed exactly one real
    /// change: <c>Structure Existing</c> became <see cref="ExistingId"/>. Nothing was lost,
    /// because <c>ImportOne</c> already re-resolves the structure by Id at write time rather than
    /// trusting a handle captured when the plan was built (the operator may have renamed the row
    /// in between).
    ///
    /// It raises <see cref="PropertyChanged"/> on the four fields the operator can edit, so the
    /// review row binds two-way straight onto the model. That replaces reading the edits back out
    /// of a grid by row index, which was silently wrong the moment anyone sorted the grid: the
    /// row order and the plan order diverged, and prompt A's contours went into the Id and DICOM
    /// type meant for prompt B.
    /// </summary>
    public class StructurePlan : INotifyPropertyChanged
    {
        private bool _selected;
        private string _structureId;
        private string _dicomType;
        private Color _color;
        private string _note;

        // --- fixed at plan time ------------------------------------------------------------ //

        /// <summary>The server's prompt, verbatim.</summary>
        public string Prompt { get; set; }

        /// <summary>
        /// The Id of the structure this prompt matched, or null when one will be created.
        /// Informational only — the write path resolves <see cref="StructureId"/> afresh.
        /// </summary>
        public string ExistingId { get; set; }

        /// <summary>Non-zero voxels the server reported.</summary>
        public long VoxelCount { get; set; }

        /// <summary>Contour entries with enough points to be worth writing.</summary>
        public int ContourCount { get; set; }

        /// <summary>Slice index range the contours span.</summary>
        public int FirstSlice { get; set; }
        public int LastSlice { get; set; }

        /// <summary>
        /// The distinct slice indices this structure actually has contours on, ascending.
        ///
        /// Not derivable from <see cref="FirstSlice"/>/<see cref="LastSlice"/>, and the difference
        /// is the point: a structure that skips slices in the middle of its range is suspicious,
        /// and the review list draws those gaps as gaps.
        /// </summary>
        public int[] OccupiedSlices { get; set; }

        /// <summary>Slices in the whole series, so occupancy can be drawn to scale.</summary>
        public int SeriesSliceCount { get; set; }

        /// <summary>Volume of one voxel in mm³, for the cm³ readout.</summary>
        public double VoxelVolumeMm3 { get; set; }

        /// <summary>
        /// Segmented volume in cm³.
        ///
        /// Shown instead of a raw voxel count because cm³ is the unit a planner already has a
        /// sense for — a liver is roughly 1200-1800 cm³ — so a wrong result is obvious. A voxel
        /// count is only meaningful after mental arithmetic with the voxel size.
        /// </summary>
        public double VolumeCc
        {
            get { return VoxelCount * VoxelVolumeMm3 / 1000.0; }
        }

        /// <summary>True when nothing came back for this prompt at all.</summary>
        public bool IsEmpty
        {
            get { return ContourCount == 0; }
        }

        /// <summary>
        /// The server found voxels but no contour survived its 10-point speckle filter.
        ///
        /// Worth its own flag rather than folding into <see cref="IsEmpty"/>: importing this row
        /// creates a structure that exists, is named, and contains nothing. That is more
        /// misleading than an obvious failure, so the review list calls it out specifically.
        /// </summary>
        public bool HasVoxelsButNoContours
        {
            get { return ContourCount == 0 && VoxelCount > 0; }
        }

        public bool WillCreate
        {
            get { return ExistingId == null; }
        }

        // --- editable by the operator ----------------------------------------------------- //

        /// <summary>Unticked structures are not written. Empty results start unticked.</summary>
        public bool Selected
        {
            get { return _selected; }
            set { if (_selected != value) { _selected = value; Raise(); } }
        }

        /// <summary>The Eclipse structure Id to write to. Capped at 16 characters.</summary>
        public string StructureId
        {
            get { return _structureId; }
            set
            {
                if (_structureId != value)
                {
                    _structureId = value;
                    Raise();
                }
            }
        }

        /// <summary>DICOM structure type. CONTROL by default; ORGAN when the planner asks.</summary>
        public string DicomType
        {
            get { return _dicomType; }
            set { if (_dicomType != value) { _dicomType = value; Raise(); } }
        }

        /// <summary>
        /// The colour the structure will be created with, assigned by
        /// <see cref="StructurePalette"/> and overridable in the review list.
        ///
        /// Ignored when overwriting an existing structure — see <c>ImportOne</c>; changing the
        /// colour of a structure the planner already built a plan around is not ours to do.
        /// </summary>
        public Color Color
        {
            get { return _color; }
            set { if (_color != value) { _color = value; Raise(); } }
        }

        /// <summary>Anything the operator needs to know before ticking this row.</summary>
        public string Note
        {
            get { return _note; }
            set { if (_note != value) { _note = value; Raise(); } }
        }

        // --- notification ----------------------------------------------------------------- //

        public event PropertyChangedEventHandler PropertyChanged;

        private void Raise([CallerMemberName] string name = null)
        {
            PropertyChangedEventHandler handler = PropertyChanged;
            if (handler != null) handler(this, new PropertyChangedEventArgs(name));
        }
    }
}
