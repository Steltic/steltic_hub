# openBIM drawings in the existing question flow

Update the User-designed building module to install its pinned openBIM dependency. **Questions** links to the drawing information requests, and **Drawings / info** opens them directly. The catalog also offers an independent openBIM module. Both use the same widget and project revision store.

Choose a completed HR/CFS job containing a physical `bim_package.json`, or upload that export. Drawing information requests have explicit proposals accepted for provisional drawing by default. They do not block PDFs. Proposed positions/sizes remain dashed and labelled `TBD - resolve query`, with their dimensions omitted.

Users can add information, quantitatively confirm an opening layout, reopen it, regenerate the set or import a revised physical package. Confirmed opening coordinates update the overlay and its schedule; prior PDFs/answers remain in revision history. Engineering/supplier notes require an updated source package to change structural geometry. No text answer silently cuts/redesigns framing or confirms an unresolved dimension.

Existing required HR build inputs, model checks and nonlinear confirmation gates retain their behavior. The shared drawing API uses the existing local request guard, source/revision checks, file locks and atomic publication. `OPENBIM_JOBS` is the shared openBIM module jobs path; `HR_JOBS` and `CFS_JOBS` are source export roots.

The initial workflow includes three reviewed pilot drawing sets. General detailing across all buildings and IFC export remain subsequent work. See [openBIM's workflow documentation](https://github.com/Steltic/steltic_openBIM/blob/main/docs/stage4/README.md).

Validation: 30 focused Hub checks passed with the openBIM dependency installed, including the real API import/answer/PDF loop and preserved model gates. Browser testing used the actual User-designed building page. Broader checks exposed three failures also reproduced on unchanged main (two Windows crash-code fixtures and a path-error wording assertion), plus timing-dependent behavior in the unchanged legacy staging test. Those unrelated runner areas are not modified here.
