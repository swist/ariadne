'use strict'
import { v1 as neo4j } from 'neo4j-driver'
export default neo4j.driver("bolt://localhost", neo4j.auth.basic("neo4j", "neo4j"))
