# data/

Local, git-ignored data (AstraZeneca NDA). Nothing in this folder except this README is committed.

- `exemplars/` - hand-written DDS `.docx` files (e.g. `Spec EM-EXAMPLE-CL.docx`). They are indexed
  as retrieval exemplars so the agents learn the house style. When a module is generated or
  evaluated, its own exemplar is excluded automatically (leave-one-out), so adding the spec for
  module X never leaks X's answers into X's run. Add every approved DDS you have; each one makes
  the agents better on the *other* modules.
