The experience makes me think:  Claude CLI is a bit of a victim of its skills.  Since it is so good at coding complex code, it doesn't naturally write using obvious simplifications.  For example, it will write with re/im instead of a complex type.  It will build macros if it needs.  It manually writes its own serialization methods instead of looking for one in the existing codebase.  The code works and passes all tests.  But I still think we should try to get AI codegen to use the built-in patterns.   The question is how?

One pattern that I was following on the llmgrader project was:

- create a corpus of all the examples and docs
- RAG the corpus
- provide MCP tools to access the RAG as well as get directed steps

At some point, we should see what examples + guidance docs + RAG model allows an AI engine to write the code in a way we would expect.
