from typing import Optional, Tuple

# conflict_type: "none", "edited_by_other", "deleted_by_other"
BackupConflict = Tuple[str, Optional[int]]
