'use strict'

class Model {
  static labels(){
    let res = [this.name]
    let proto = Object.getPrototypeOf(this)
    
    while (proto.name !== 'Model'){
      res.push(proto.name)
      proto = Object.getPrototypeOf(proto)
    }

    return res
  }
}

export default Model
